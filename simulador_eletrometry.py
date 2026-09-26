#!/usr/bin/env python3
"""Eletrometry 1.0 — cabines trifasicas, corrente 1 s e temperatura 30 s.

Python 3.9+. Saida local so usa biblioteca padrao (tzdata pode ser necessario
no Windows). IoT Core HTTPS usa boto3. Consulte LEIA_ME.md antes do uso.
O modelo e sintetico, parametrico e nao representa uma certificacao eletrica.
"""
import argparse
import gzip
import hashlib
import json
import logging
import math
import random
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = '1.0.0'
PHASES = ('A', 'B', 'C')
LOG = logging.getLogger('eletrometry')
DEFAULT = {
    'planta_id': 'planta-01', 'fuso_operacao': 'America/Sao_Paulo',
    'corrente_nominal_a': 400.0, 'tc_fundo_escala_a': 600.0,
    'modelo_tc': 'TC generico virtual 600 A; selecao fisica pendente',
    'carga_dia_pu': 0.62, 'carga_noite_pu': 0.24,
    'hora_inicio_turno': 6.0, 'hora_fim_turno': 22.0,
    'fator_fim_semana': 0.72, 'ambiente_medio_c': 27.0,
    'amplitude_diaria_c': 3.0, 'amplitude_sazonal_c': 2.0,
    'elevacao_nominal_c': 40.0, 'tau_termico_s': 900.0,
    'tau_sensor_temperatura_s': 12.0,
    'temperatura_min_sensor_c': -50.0, 'temperatura_max_sensor_c': 200.0,
    'ruido_corrente_relativo': 0.0015, 'ruido_temperatura_c': 0.06,
}
SCENARIOS = {'sobrecarga', 'desequilibrio', 'mau_contato', 'desligamento',
             'sensor_sem_leitura', 'sensor_congelado', 'sem_comunicacao'}


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def utc_text(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse_start(text):
    dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('--inicio exige fuso, por exemplo 2026-09-28T08:00:00-03:00')
    if dt.microsecond:
        raise ValueError('--inicio deve ter precisao de segundos, sem fracao.')
    return dt.astimezone(timezone.utc)


def stable_seed(seed, cabine_id, domain):
    raw = f'{seed}:{cabine_id}:{domain}'.encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], 'big')


def numeric(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name}: informe um numero.')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name}: esperado entre {low} e {high}.')


def load_config(path):
    conf = dict(DEFAULT)
    if path:
        custom = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(custom, dict) or set(custom) - set(DEFAULT):
            raise ValueError('Configuracao invalida ou campos desconhecidos.')
        conf.update(custom)
    if not isinstance(conf['planta_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]+', conf['planta_id']):
        raise ValueError('planta_id deve conter apenas letras, numeros, _ e -.')
    if not isinstance(conf['modelo_tc'], str):
        raise ValueError('modelo_tc deve ser texto.')
    bounds = {
        'corrente_nominal_a': (1, 2500), 'tc_fundo_escala_a': (1, 5000),
        'carga_dia_pu': (0, 0.85), 'carga_noite_pu': (0, 0.85),
        'hora_inicio_turno': (0, 23.99), 'hora_fim_turno': (0.01, 24),
        'fator_fim_semana': (0, 1), 'ambiente_medio_c': (5, 45),
        'amplitude_diaria_c': (0, 10), 'amplitude_sazonal_c': (0, 8),
        'elevacao_nominal_c': (10, 60), 'tau_termico_s': (60, 7200),
        'tau_sensor_temperatura_s': (1, 120),
        'temperatura_min_sensor_c': (-100, 0), 'temperatura_max_sensor_c': (100, 300),
        'ruido_corrente_relativo': (0, 0.01), 'ruido_temperatura_c': (0, 0.5),
    }
    for key, (low, high) in bounds.items():
        numeric(conf[key], key, low, high)
    if conf['tc_fundo_escala_a'] < conf['corrente_nominal_a']:
        raise ValueError('O fundo de escala do TC deve cobrir a corrente nominal da cabine.')
    if conf['hora_inicio_turno'] >= conf['hora_fim_turno']:
        raise ValueError('Neste modelo, o turno deve iniciar antes de terminar no mesmo dia.')
    ZoneInfo(conf['fuso_operacao'])
    return conf


def load_scenarios(path, ids):
    events = [] if path is None else json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(events, list):
        raise ValueError('O arquivo de cenarios deve ser uma lista JSON.')
    common = {'id', 'cabine_id', 'tipo', 'inicio_s', 'duracao_s'}
    extras = {
        'sobrecarga': {'multiplicador', 'rampa_s'},
        'desequilibrio': {'fase', 'fracao', 'rampa_s'},
        'mau_contato': {'fase', 'fator_resistencia', 'rampa_s'},
        'desligamento': set(), 'sem_comunicacao': set(),
        'sensor_sem_leitura': {'fase', 'grandeza'},
        'sensor_congelado': {'fase', 'grandeza'},
    }
    seen = set()
    for e in events:
        if not isinstance(e, dict) or not common <= set(e):
            raise ValueError('Cada cenario exige id, cabine_id, tipo, inicio_s e duracao_s.')
        kind = e['tipo']
        if kind not in SCENARIOS or set(e) - (common | extras[kind]):
            raise ValueError(f'Cenario desconhecido ou campos invalidos: {kind}')
        if not isinstance(e['id'], str) or not e['id'] or e['id'] in seen:
            raise ValueError('IDs de cenarios devem ser textos unicos e nao vazios.')
        seen.add(e['id'])
        if e['cabine_id'] != '*' and e['cabine_id'] not in ids:
            raise ValueError(f'Cenario {e["id"]}: cabine ausente nesta execucao.')
        for key in ('inicio_s', 'duracao_s'):
            if type(e[key]) is not int or e[key] < (1 if key == 'duracao_s' else 0):
                raise ValueError(f'{key} deve ser inteiro nao negativo (duracao > 0).')
        if 'fase' in extras[kind] and e.get('fase') not in PHASES:
            raise ValueError(f'Cenario {e["id"]}: fase deve ser A, B ou C.')
        if 'grandeza' in extras[kind] and e.get('grandeza') not in ('corrente', 'temperatura'):
            raise ValueError('grandeza deve ser corrente ou temperatura.')
        if 'rampa_s' in extras[kind]:
            e.setdefault('rampa_s', 60)
            numeric(e['rampa_s'], 'rampa_s', 1, e['duracao_s'])
        for field, limits, default in [
            ('multiplicador', (1.01, 1.30), 1.18),
            ('fracao', (0.05, 0.60), 0.35),
            ('fator_resistencia', (1.1, 5.0), 4.0),
        ]:
            if field in extras[kind]:
                e.setdefault(field, default)
                numeric(e[field], field, *limits)
    # Nao combinar falhas: evita inventar interacoes fisicas nao modeladas.
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            same = a['cabine_id'] == '*' or b['cabine_id'] == '*' or a['cabine_id'] == b['cabine_id']
            overlap = max(a['inicio_s'], b['inicio_s']) < min(
                a['inicio_s'] + a['duracao_s'], b['inicio_s'] + b['duracao_s'])
            if same and overlap:
                raise ValueError(f'Cenarios sobrepostos na mesma cabine: {a["id"]}, {b["id"]}.')
    return events


class Cabine:
    """Estado fisico simplificado, independente do transporte e dos limites de alerta."""
    def __init__(self, cabine_id, config, seed, start, scenarios):
        self.id, self.c, self.tz = cabine_id, config, ZoneInfo(config['fuso_operacao'])
        self.rng = random.Random(stable_seed(seed, cabine_id, 'fisica'))
        r = random.Random(stable_seed(seed, cabine_id, 'perfil'))
        self.measure_rng = {kind: random.Random(stable_seed(seed, cabine_id, kind))
                            for kind in ('corrente', 'temperatura')}
        self.load_scale = r.uniform(0.92, 1.06)
        uncentered = [r.uniform(-0.02, 0.02) for _ in PHASES]
        avg = sum(uncentered) / 3
        self.phase_scale = [1 + x - avg for x in uncentered]
        self.cycle_period = r.uniform(180, 480)
        self.cycle_offset = r.uniform(0, 2 * math.pi)
        self.ambient_offset = r.uniform(-0.8, 0.8)
        self.taus = [config['tau_termico_s'] * r.uniform(0.9, 1.1) for _ in PHASES]
        self.gain = [r.uniform(-0.004, 0.004) for _ in PHASES]
        self.temp_bias = [r.uniform(-0.15, 0.15) for _ in PHASES]
        self.noise = 0.0
        self.events = [e for e in scenarios if e['cabine_id'] in ('*', cabine_id)]
        self.active = None
        self.frozen = {}
        self.last_measured = {}
        self.ambient = self.ambient_at(start)
        fraction = self.normal_load(start)
        self.current = [config['corrente_nominal_a'] * fraction * s for s in self.phase_scale]
        ratio2 = [(i / config['corrente_nominal_a']) ** 2 for i in self.current]
        mean2 = sum(ratio2) / 3
        self.temperature = [self.ambient + config['elevacao_nominal_c'] * (0.9 * x + 0.1 * mean2)
                            for x in ratio2]
        self.sensor_temperature = list(self.temperature)

    def ambient_at(self, dt):
        local = dt.astimezone(self.tz)
        hour = local.hour + local.minute / 60 + local.second / 3600
        # Maximo diario aproximado as 15h; maximo sazonal aproximado em janeiro.
        return (self.c['ambiente_medio_c'] + self.ambient_offset
                + self.c['amplitude_diaria_c'] * math.cos(2 * math.pi * (hour - 15) / 24)
                + self.c['amplitude_sazonal_c'] * math.cos(2 * math.pi * (local.timetuple().tm_yday - 20) / 365.25))

    def normal_load(self, dt):
        local = dt.astimezone(self.tz)
        hour = local.hour + local.minute / 60 + local.second / 3600
        def rise(x):
            # Rampa suave de 30 minutos na troca de turno.
            x = max(0.0, min(1.0, x))
            return x * x * (3 - 2 * x)
        day = rise((hour - self.c['hora_inicio_turno']) / 0.5)
        day *= 1 - rise((hour - self.c['hora_fim_turno']) / 0.5)
        load = self.c['carga_noite_pu'] + day * (self.c['carga_dia_pu'] - self.c['carga_noite_pu'])
        if local.weekday() >= 5:
            load *= self.c['fator_fim_semana']
        cycle = 0.025 * math.sin(2 * math.pi * dt.timestamp() / self.cycle_period + self.cycle_offset)
        return max(0.0, min(0.93, load * self.load_scale + cycle + self.noise))

    def step(self, dt, elapsed):
        self.active = next((e for e in self.events if e['inicio_s'] <= elapsed < e['inicio_s'] + e['duracao_s']), None)
        self.noise = 0.985 * self.noise + math.sqrt(1 - 0.985 ** 2) * self.rng.gauss(0, 0.008)
        self.ambient = self.ambient_at(dt)
        target = [self.c['corrente_nominal_a'] * self.normal_load(dt) * x for x in self.phase_scale]
        resistance = [1.0] * 3
        e = self.active
        if e:
            ramp = min(1.0, (elapsed - e['inicio_s'] + 1) / e.get('rampa_s', 1))
            if e['tipo'] == 'sobrecarga':
                high = [self.c['corrente_nominal_a'] * e['multiplicador'] * x for x in self.phase_scale]
                target = [a + ramp * (b - a) for a, b in zip(target, high)]
            elif e['tipo'] == 'desequilibrio':
                phase = PHASES.index(e['fase'])
                delta = sum(target) / 3 * e['fracao'] * ramp
                target = [v + (delta if j == phase else -delta / 2) for j, v in enumerate(target)]
            elif e['tipo'] == 'mau_contato':
                resistance[PHASES.index(e['fase'])] = 1 + ramp * (e['fator_resistencia'] - 1)
            elif e['tipo'] == 'desligamento':
                target = [0.0] * 3
        if e and e['tipo'] == 'desligamento':
            self.current = [0.0] * 3
        else:
            # Resposta da carga em segundos; nao e a forma de onda de 60 Hz.
            self.current = [max(0.0, v + (t - v) * (1 - math.exp(-1 / 3)))
                            for v, t in zip(self.current, target)]
        ratio2 = [(i / self.c['corrente_nominal_a']) ** 2 for i in self.current]
        avg2 = sum(ratio2) / 3
        for j in range(3):
            target_t = self.ambient + self.c['elevacao_nominal_c'] * (0.9 * resistance[j] * ratio2[j] + 0.1 * avg2)
            self.temperature[j] += (target_t - self.temperature[j]) * (1 - math.exp(-1 / self.taus[j]))
            self.sensor_temperature[j] += (self.temperature[j] - self.sensor_temperature[j]) * (
                1 - math.exp(-1 / self.c['tau_sensor_temperatura_s']))

    def measure(self, kind):
        values, quality = {}, {}
        rng = self.measure_rng[kind]
        for j, phase in enumerate(PHASES):
            key = (kind, phase)
            if kind == 'corrente':
                value = self.current[j] * (1 + self.gain[j])
                value = max(0.0, value + rng.gauss(0, abs(value) * self.c['ruido_corrente_relativo'] + 0.03))
                if self.current[j] == 0 and value < 0.1:
                    value = 0.0
                low, high, digits = 0, self.c['tc_fundo_escala_a'], 2
            else:
                value = self.sensor_temperature[j] + self.temp_bias[j] + rng.gauss(0, self.c['ruido_temperatura_c'])
                low, high, digits = self.c['temperatura_min_sensor_c'], self.c['temperatura_max_sensor_c'], 1
            q = 'ok'
            # Nunca apresenta uma leitura fora da faixa como um valor confiavel.
            if not low <= value <= high:
                value, q = None, 'fora_faixa'
            else:
                value = round(value, digits)
            e = self.active
            if e and e.get('grandeza') == kind and e.get('fase') == phase:
                if e['tipo'] == 'sensor_sem_leitura':
                    value, q = None, 'sem_leitura'
                elif e['tipo'] == 'sensor_congelado':
                    # Falha silenciosa: o firmware nao conhece a causa injetada.
                    freeze_key = (e['id'], kind, phase)
                    if freeze_key not in self.frozen:
                        self.frozen[freeze_key] = self.last_measured.get(key, (value, q))
                    value, q = self.frozen[freeze_key]
            values[phase], quality[phase] = value, q
            self.last_measured[key] = (value, q)
        return values, quality

    def metadata(self):
        return {'cabine_id': self.id, 'corrente_nominal_a': self.c['corrente_nominal_a'],
                'tc_fundo_escala_a': self.c['tc_fundo_escala_a'], 'fator_carga_individual': self.load_scale,
                'fatores_fase': dict(zip(PHASES, self.phase_scale)),
                'tau_termico_s_por_fase': dict(zip(PHASES, self.taus))}


def messages(cabine, dt, elapsed, run_id, executor, config_id):
    kinds = ['corrente', 'temperatura'] if elapsed % 30 == 0 else ['corrente']
    for kind in kinds:
        # Amostragem continua mesmo sem comunicacao; pacotes desse cenario sao
        # perdidos intencionalmente. Falha real no envio usa a fila persistente.
        values, quality = cabine.measure(kind)
        if cabine.active and cabine.active['tipo'] == 'sem_comunicacao':
            continue
        interval = 1 if kind == 'corrente' else 30
        yield {
            'schema_version': 1, 'simulator_version': VERSION,
            'run_id': run_id, 'event_id': f'{run_id}:{cabine.id}:{kind}:{elapsed // interval}',
            'executor_id': executor, 'planta_id': cabine.c['planta_id'],
            'cabine_id': cabine.id, 'device_id': f'iot-{cabine.id}', 'config_id': config_id,
            'simulated': True, 'timestamp': utc_text(dt), 'sequence': elapsed // interval,
            'intervalo_s': interval, 'tipo': kind,
            'unidade': 'A_rms' if kind == 'corrente' else 'degC',
            'valores': values, 'qualidade': quality,
        }


class Archive:
    def __init__(self, root, limit=20000):
        self.root, self.limit = Path(root), limit
        self.handles, self.index, self.count = {}, Counter(), 0

    def write(self, message):
        key = (message['timestamp'][:10], message['timestamp'][11:13], message['tipo'])
        kind = message['tipo']
        existing = self.handles.get(kind)
        if existing and (existing[0] != key or existing[2] >= self.limit):
            existing[1].close()
            del self.handles[kind]
        if kind not in self.handles:
            self.index[key] += 1
            folder = self.root / f'data={key[0]}' / f'hora={key[1]}' / f'tipo={kind}'
            folder.mkdir(parents=True, exist_ok=True)
            stream = gzip.open(folder / f'lote-{self.index[key]:05d}.jsonl.gz', 'xt', encoding='utf-8', compresslevel=3)
            self.handles[kind] = [key, stream, 0]
        entry = self.handles[kind]
        entry[1].write(encode(message) + '\n')
        entry[2] += 1
        self.count += 1
        if entry[2] % 1000 == 0:
            entry[1].flush()

    def close(self):
        for entry in self.handles.values():
            entry[1].close()
        self.handles.clear()


class Outbox:
    def __init__(self, path, region, limit=50000):
        self.path, self.region, self.limit = Path(path), region, limit
        self.db = self.connect()
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload TEXT NOT NULL)')
        row = self.db.execute("SELECT value FROM meta WHERE key='region'").fetchone()
        if row and row[0] != region:
            raise ValueError('A regiao nao corresponde a fila local de pendencias.')
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('region', ?)", (region,))
        self.db.commit()

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def count(self):
        return self.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0]

    def enqueue(self, pairs):
        if self.count() + len(pairs) > self.limit:
            raise RuntimeError('Fila local atingiu o limite. Geracao interrompida; reenvie as pendencias antes de continuar.')
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO pending VALUES (?, ?, ?)',
                                [(m['event_id'], topic, encode(m)) for topic, m in pairs])

    def close(self):
        self.db.close()


class Publisher:
    def __init__(self, region, profile, endpoint):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError('Envio AWS exige: python -m pip install boto3') from exc
        session = boto3.Session(region_name=region, profile_name=profile)
        cfg = Config(connect_timeout=5, read_timeout=10,
                     retries={'mode': 'standard', 'total_max_attempts': 2})
        if not endpoint:
            endpoint = session.client('iot', config=cfg).describe_endpoint(endpointType='iot:Data-ATS')['endpointAddress']
        if not re.fullmatch(r'[A-Za-z0-9-]+\.iot\.' + re.escape(region) + r'\.amazonaws\.com', endpoint):
            raise ValueError('--endpoint deve ser somente o hostname IoT ATS da regiao escolhida.')
        self.client = session.client('iot-data', endpoint_url=f'https://{endpoint}', config=cfg)

    def publish(self, topic, payload):
        self.client.publish(topic=topic, qos=1, retain=False, payload=payload.encode('utf-8'))


class Sender(threading.Thread):
    def __init__(self, outbox, publisher, rate):
        super().__init__(daemon=True)
        self.outbox, self.publisher, self.rate = outbox, publisher, rate
        self.stop_event, self.fatal = threading.Event(), None
        self.sent = 0

    def run(self):
        db = self.outbox.connect()
        delay, next_send = 1.0, 0.0
        try:
            while not self.stop_event.is_set():
                row = db.execute('SELECT id, topic, payload FROM pending ORDER BY rowid LIMIT 1').fetchone()
                if not row:
                    self.stop_event.wait(0.1)
                    continue
                if self.stop_event.wait(max(0, next_send - time.monotonic())):
                    break
                try:
                    self.publisher.publish(row[1], row[2])
                    with db:
                        db.execute('DELETE FROM pending WHERE id=?', (row[0],))
                    self.sent += 1
                    delay = 1.0
                    next_send = time.monotonic() + 1 / self.rate
                except Exception as exc:
                    code = getattr(exc, 'response', {}).get('Error', {}).get('Code', type(exc).__name__)
                    if code in {'ExpiredToken', 'ExpiredTokenException', 'AccessDenied', 'AccessDeniedException',
                                'UnauthorizedException', 'UnrecognizedClientException', 'InvalidClientTokenId',
                                'NoCredentialsError', 'PartialCredentialsError'}:
                        self.fatal = f'{code}: corrija a sessao/permissao e use reenviar. Mensagens preservadas.'
                        return
                    LOG.warning('Publicacao falhou (%s); nova tentativa em %.0f s.', code, delay)
                    self.stop_event.wait(delay)
                    delay = min(30.0, delay * 2)
        except Exception as exc:
            self.fatal = f'Falha na fila local: {type(exc).__name__}: {exc}'
        finally:
            db.close()

    def finish(self, timeout):
        deadline = time.monotonic() + timeout
        try:
            while self.outbox.count() and not self.fatal and self.is_alive() and time.monotonic() < deadline:
                time.sleep(0.2)
        finally:
            self.stop_event.set()
            self.join(timeout=35)
        return self.outbox.count()


def aws_arguments(parser):
    parser.add_argument('--regiao', default='us-east-1', choices=['us-east-1', 'us-west-2'])
    parser.add_argument('--profile', help='Perfil AWS local; omitir no CloudShell/EC2 com role.')
    parser.add_argument('--endpoint', help='Hostname IoT ATS opcional; evita DescribeEndpoint.')
    parser.add_argument('--taxa-envio', type=float, default=20, help='Limite global de publicacoes/s deste processo.')
    parser.add_argument('--espera-envio', type=int, default=30, help='Segundos para esvaziar fila ao finalizar.')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('simular', help='Gerar dados e, opcionalmente, publicar no IoT Core.')
    p.add_argument('--config', help='Perfil fisico em JSON; sem arquivo usa 400 A / TC 600 A.')
    p.add_argument('--cenarios', help='Falhas programadas em JSON; omitido = operacao normal.')
    p.add_argument('--cabines', type=int, default=3)
    p.add_argument('--primeira-cabine', type=int, default=1)
    p.add_argument('--executor', default='local-01')
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--inicio', help='Data ISO com fuso; omitido = agora em UTC.')
    p.add_argument('--duracao', type=int, default=600, help='Segundos simulados; fim exclusivo. Padrao: 600.')
    p.add_argument('--continuo', action='store_true', help='Executar ate Ctrl+C, somente em tempo real.')
    p.add_argument('--modo', choices=['tempo-real', 'acelerado'], default='tempo-real')
    p.add_argument('--destino', choices=['arquivo', 'iot'], default='arquivo')
    p.add_argument('--saida', default='dados')
    p.add_argument('--prefixo-topico', default='eletrometry/demo', help='Mantem compatibilidade com a regra ja testada.')
    p.add_argument('--basic-ingest-rule', help='Opcional: nome de regra existente; publica no topico reservado.')
    p.add_argument('--limite-pendencias', type=int, default=50000)
    aws_arguments(p)
    q = subs.add_parser('reenviar', help='Publicar pendencias preservando IDs e horarios de coleta.')
    q.add_argument('--banco', required=True, help='Caminho do pendencias.sqlite3 de uma execucao.')
    aws_arguments(q)
    return parser


def validate_args(args):
    numeric(args.taxa_envio, '--taxa-envio', 0.1, 1000)
    numeric(args.espera_envio, '--espera-envio', 0, 3600)
    if args.command == 'reenviar':
        if not Path(args.banco).is_file():
            raise ValueError('Arquivo de pendencias nao encontrado.')
        return
    numeric(args.cabines, '--cabines', 1, 1000)
    numeric(args.primeira_cabine, '--primeira-cabine', 1, 999999)
    numeric(args.duracao, '--duracao', 1, 366 * 86400 * 10)
    numeric(args.limite_pendencias, '--limite-pendencias', 100, 500000)
    if args.continuo and args.modo != 'tempo-real':
        raise ValueError('--continuo so pode ser usado em tempo real.')
    if args.destino == 'iot' and args.modo != 'tempo-real':
        raise ValueError('Modo acelerado grava somente arquivos locais; nao envie historico em massa ao IoT.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.executor):
        raise ValueError('--executor invalido; use letras, numeros, _ ou -.')
    if not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_-]+', args.prefixo_topico):
        raise ValueError('--prefixo-topico exige dois niveis, exemplo eletrometry/demo.')
    if args.basic_ingest_rule and not re.fullmatch(r'[A-Za-z0-9_]+', args.basic_ingest_rule):
        raise ValueError('Nome de regra Basic Ingest invalido.')
    if args.destino == 'iot' and args.taxa_envio < args.cabines * (1 + 1 / 30):
        raise ValueError('Taxa de envio insuficiente: reduza cabines ou aumente --taxa-envio apos conferir quotas e creditos.')


def simulate(args):
    conf = load_config(args.config)
    start = parse_start(args.inicio) if args.inicio else datetime.now(timezone.utc).replace(microsecond=0)
    ids = [f'cabine-{i:03d}' for i in range(args.primeira_cabine, args.primeira_cabine + args.cabines)]
    scenarios = load_scenarios(args.cenarios, set(ids))
    cabines = [Cabine(cid, conf, args.seed, start, scenarios) for cid in ids]
    config_id = hashlib.sha256(encode({'config': conf, 'seed': args.seed, 'version': VERSION}).encode()).hexdigest()[:16]
    run_id = f'{args.executor}-{uuid.uuid4().hex[:16]}'
    root = Path(args.saida).resolve() / run_id
    root.mkdir(parents=True, exist_ok=False)
    manifest = {
        'simulator_version': VERSION, 'run_id': run_id, 'config_id': config_id,
        'seed': args.seed, 'inicio_utc': utc_text(start), 'configuracao': conf,
        'cabines': [c.metadata() for c in cabines],
        'coleta_s': {'corrente': 1, 'temperatura': 30}, 'argumentos': vars(args),
        'modelo_validado_em_campo': False,
    }
    (root / 'manifesto.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'cenarios_referencia.json').write_text(json.dumps(scenarios, ensure_ascii=False, indent=2), encoding='utf-8')
    LOG.info('Execucao %s | saida %s', run_id, root)
    if not args.continuo:
        expected = args.cabines * (args.duracao + (args.duracao + 29) // 30)
        LOG.info('Ate %s mensagens / %s valores individuais, antes de falhas de comunicacao.', expected, expected * 3)
    LOG.info('Corrente nominal %.0f A; faixa do TC %.0f A; perfil sintetico nao calibrado em campo.',
             conf['corrente_nominal_a'], conf['tc_fundo_escala_a'])
    archive = Archive(root / 'telemetria')
    outbox = sender = None
    count, tick, completed, interrupted, failure = Counter(), 0, 0, False, None
    try:
        if args.destino == 'iot':
            outbox = Outbox(root / 'pendencias.sqlite3', args.regiao, args.limite_pendencias)
            publisher = Publisher(args.regiao, args.profile, args.endpoint)
            sender = Sender(outbox, publisher, args.taxa_envio)
            sender.start()
        wall_start = time.monotonic()
        last_log = wall_start
        while args.continuo or tick < args.duracao:
            if sender and sender.fatal:
                raise RuntimeError(sender.fatal)
            if args.modo == 'tempo-real':
                time.sleep(max(0, wall_start + tick - time.monotonic()))
            dt = start + timedelta(seconds=tick)
            pairs = []
            for cabine in cabines:
                cabine.step(dt, tick)
                for message in messages(cabine, dt, tick, run_id, args.executor, config_id):
                    topic = f'{args.prefixo_topico}/{cabine.id}/{message["tipo"]}'
                    if args.basic_ingest_rule:
                        topic = f'$aws/rules/{args.basic_ingest_rule}/{topic}'
                    pairs.append((topic, message))
            if outbox:
                outbox.enqueue(pairs)
            for _, message in pairs:
                archive.write(message)
                count[message['tipo']] += 1
            tick += 1
            completed = tick
            if time.monotonic() - last_log >= 10:
                lag = max(0, time.monotonic() - wall_start - tick) if args.modo == 'tempo-real' else 0
                LOG.info('Tempo simulado %ss | mensagens %s | pendentes %s | atraso %.1fs',
                         tick, sum(count.values()), outbox.count() if outbox else 0, lag)
                last_log = time.monotonic()
    except KeyboardInterrupt:
        interrupted = True
        LOG.info('Interrompido pelo usuario; finalizando arquivos e fila local.')
    except Exception as exc:
        failure = exc
    finally:
        archive.close()
        pending, sent = 0, 0
        if sender and sender.ident is not None:
            try:
                pending = sender.finish(args.espera_envio)
            except KeyboardInterrupt:
                sender.stop_event.set()
                sender.join(timeout=35)
                pending = outbox.count()
            sent = sender.sent
            if sender.fatal and failure is None:
                failure = RuntimeError(sender.fatal)
        elif outbox:
            pending = outbox.count()
        if outbox:
            outbox.close()
        summary = {'mensagens_arquivadas': dict(count), 'segundos_concluidos': completed,
                   'mensagens_aceitas_iot': sent, 'pendencias': pending, 'interrompido': interrupted,
                   'erro': None if failure is None else str(failure)}
        (root / 'resumo.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    LOG.info('Final: %s | pasta %s', encode(summary), root)
    if pending:
        LOG.warning('Reenvie com: python simulador_eletrometry.py reenviar --banco "%s" --regiao %s',
                    root / 'pendencias.sqlite3', args.regiao)
    if args.destino == 'iot':
        LOG.info('Aceite pelo IoT Core nao confirma entrega ao SQS; verifique a regra e o consumidor.')
    if failure:
        raise failure
    return 2 if pending else (130 if interrupted else 0)


def resend(args):
    outbox = Outbox(args.banco, args.regiao)
    sender = None
    try:
        if not outbox.count():
            LOG.info('Nenhuma mensagem pendente.')
            return 0
        LOG.info('Reenviando %s mensagens; IDs e timestamps originais preservados.', outbox.count())
        sender = Sender(outbox, Publisher(args.regiao, args.profile, args.endpoint), args.taxa_envio)
        sender.start()
        pending = sender.finish(args.espera_envio)
        LOG.info('Aceitas pelo IoT: %s | pendentes: %s', sender.sent, pending)
        if sender.fatal:
            raise RuntimeError(sender.fatal)
        return 2 if pending else 0
    finally:
        if sender and sender.is_alive():
            sender.stop_event.set()
            sender.join(timeout=35)
        outbox.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = build_parser()
    args = parser.parse_args()
    try:
        validate_args(args)
        return simulate(args) if args.command == 'simular' else resend(args)
    except ZoneInfoNotFoundError:
        LOG.error('Fuso nao encontrado. No Windows instale: python -m pip install tzdata')
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        LOG.error('%s: %s', type(exc).__name__, exc)
    return 1


if __name__ == '__main__':
    sys.exit(main())
