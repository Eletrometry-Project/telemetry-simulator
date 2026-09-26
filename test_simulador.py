"""Verificacoes locais sem credenciais, rede ou chamadas AWS."""
import gzip
import json
import math
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

import simulador_eletrometry as sim

START = sim.parse_start('2026-09-28T08:00:00-03:00')


def event(kind, **kw):
    return dict(id='evento', cabine_id='cabine-001', tipo=kind,
                inicio_s=600, duracao_s=1800, **kw)


def simulate_pair(events, ticks=3000):
    a = sim.Cabine('cabine-001', sim.load_config(None), 42, START, [])
    b = sim.Cabine('cabine-001', sim.load_config(None), 42, START, events)
    snapshots = {}
    for t in range(ticks):
        dt = START + timedelta(seconds=t)
        a.step(dt, t); b.step(dt, t)
        if t in (599, 600, 1200, 1800, 2399, 2400, ticks - 1):
            snapshots[t] = (list(a.current), list(b.current), list(a.temperature), list(b.temperature))
    return snapshots


class ModelTests(unittest.TestCase):
    def test_cadence_and_contract(self):
        c = sim.Cabine('cabine-001', sim.load_config(None), 42, START, [])
        data = []
        for t in range(31):
            dt = START + timedelta(seconds=t); c.step(dt, t)
            data.extend(sim.messages(c, dt, t, 'run', 'exec', 'config'))
        self.assertEqual(len(data), 33)
        temps = [x for x in data if x['tipo'] == 'temperatura']
        self.assertEqual([x['sequence'] for x in temps], [0, 1])
        self.assertEqual(temps[1]['timestamp'], '2026-09-28T11:00:30Z')
        self.assertEqual(len({x['event_id'] for x in data}), 33)
        for x in data:
            self.assertEqual(set(x['valores']), set(sim.PHASES))
            self.assertNotIn('cenario', x)
            self.assertIn('cabine_id', x)
            json.dumps(x, allow_nan=False)

    def test_reproducible_and_independent_of_other_cabines(self):
        c1 = sim.Cabine('cabine-003', sim.load_config(None), 7, START, [])
        c2 = sim.Cabine('cabine-003', sim.load_config(None), 7, START, [])
        other = sim.Cabine('cabine-002', sim.load_config(None), 7, START, [])
        for t in range(100):
            dt = START + timedelta(seconds=t)
            c1.step(dt, t); other.step(dt, t); c2.step(dt, t)
            self.assertEqual(list(sim.messages(c1, dt, t, 'r', 'e', 'c')),
                             list(sim.messages(c2, dt, t, 'r', 'e', 'c')))
        self.assertNotEqual(c1.current, other.current)

    def test_normal_day_plausibility(self):
        c = sim.Cabine('cabine-001', sim.load_config(None), 42, START, [])
        prev = c.temperature[:]
        currents, temps, max_d = [], [], 0
        for t in range(86400):
            c.step(START + timedelta(seconds=t), t)
            max_d = max(max_d, max(abs(x-y) for x,y in zip(prev, c.temperature)))
            prev = c.temperature[:]
            if t % 30 == 0:
                currents.extend(c.current); temps.extend(c.temperature)
            for v in c.current + c.temperature:
                self.assertTrue(math.isfinite(v))
        self.assertGreater(min(currents), 40)
        self.assertLess(max(currents), 330)
        self.assertGreater(min(temps), 15)
        self.assertLess(max(temps), 65)
        self.assertLess(max_d, 0.1)

    def test_overload_increases_current_and_delayed_heat(self):
        s = simulate_pair([event('sobrecarga', multiplicador=1.18, rampa_s=60)])
        self.assertGreater(s[1200][1][0], 450)
        self.assertGreater(s[2399][3][0], s[2399][2][0] + 20)
        self.assertLess(abs(s[600][3][0] - s[599][3][0]), 0.1)
        self.assertLess(s[2999][3][0], s[2399][3][0])

    def test_contact_heat_without_current_increase(self):
        s = simulate_pair([event('mau_contato', fase='B', fator_resistencia=4.0, rampa_s=60)])
        self.assertEqual(s[1800][0], s[1800][1])
        self.assertGreater(s[2399][3][1], s[2399][2][1] + 20)
        self.assertAlmostEqual(s[2399][3][0], s[2399][2][0])

    def test_imbalance_preserves_sum_of_currents(self):
        s = simulate_pair([event('desequilibrio', fase='B', fracao=0.35, rampa_s=60)])
        normal, fault = s[1800][:2]
        self.assertAlmostEqual(sum(normal), sum(fault), places=7)
        self.assertGreater(fault[1], normal[1] * 1.3)

    def test_shutdown_zero_current_and_cooling(self):
        s = simulate_pair([event('desligamento')])
        self.assertEqual(s[600][1], [0, 0, 0])
        self.assertLess(s[600][3][0], s[599][3][0])
        self.assertGreater(s[600][3][0], 30)
        self.assertLess(s[1800][3][0], s[600][3][0])

    def test_sensor_fault_and_communication_gap(self):
        for kind in ('sensor_sem_leitura', 'sensor_congelado', 'sem_comunicacao'):
            e = dict(id='x', cabine_id='cabine-001', tipo=kind, inicio_s=30, duracao_s=60)
            if kind != 'sem_comunicacao':
                e.update(fase='A', grandeza='temperatura')
            c = sim.Cabine('cabine-001', sim.load_config(None), 42, START, [e])
            data = []
            for t in range(91):
                dt = START + timedelta(seconds=t); c.step(dt, t)
                data.extend(sim.messages(c, dt, t, 'r', 'e', 'c'))
            temps = [m for m in data if m['tipo'] == 'temperatura']
            if kind == 'sem_comunicacao':
                self.assertEqual([x['sequence'] for x in temps], [0, 3])
            elif kind == 'sensor_sem_leitura':
                self.assertIsNone(temps[1]['valores']['A'])
                self.assertEqual(temps[1]['qualidade']['A'], 'sem_leitura')
            else:
                self.assertEqual(temps[0]['valores']['A'], temps[1]['valores']['A'])
                self.assertEqual(temps[1]['valores']['A'], temps[2]['valores']['A'])
                self.assertEqual(temps[1]['qualidade']['A'], 'ok')

    def test_overrange_is_null_and_flagged(self):
        c = sim.Cabine('cabine-001', sim.load_config(None), 42, START, [])
        c.current[0] = 700
        value, quality = c.measure('corrente')
        self.assertIsNone(value['A']); self.assertEqual(quality['A'], 'fora_faixa')

    def test_configs_and_scenarios(self):
        folder = Path(__file__).parent
        for name in ('config_industrial.json', 'config_sct013_100a.json'):
            sim.load_config(folder / name)
        events = sim.load_scenarios(folder / 'cenarios_exemplo.json', {'cabine-001','cabine-002','cabine-003'})
        self.assertEqual(len(events), 7)

    def test_archive_rotation_and_counts(self):
        with tempfile.TemporaryDirectory() as d:
            archive = sim.Archive(d, limit=2)
            for t in range(5):
                archive.write({'timestamp': '2026-09-28T11:00:00Z', 'tipo': 'corrente', 'seq': t})
            archive.close()
            files = list(Path(d).rglob('*.gz'))
            self.assertEqual(len(files), 3)
            rows = [json.loads(line) for f in files for line in gzip.open(f, 'rt')]
            self.assertEqual(sorted(x['seq'] for x in rows), list(range(5)))

    def test_outbox_survives_failure_and_replay_keeps_id(self):
        class Fails:
            def publish(self, *args):
                e = RuntimeError('expired')
                e.response = {'Error': {'Code': 'ExpiredToken'}}
                raise e
        class Success:
            def __init__(self): self.seen = []
            def publish(self, topic, payload): self.seen.append(json.loads(payload))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'pending.sqlite3'
            o = sim.Outbox(p, 'us-east-1')
            msg = {'event_id':'unique', 'timestamp':'2026-09-28T11:00:00Z'}
            o.enqueue([('topic', msg), ('topic', msg)])
            self.assertEqual(o.count(), 1)
            s = sim.Sender(o, Fails(), 100); s.start(); s.join(2)
            self.assertIsNotNone(s.fatal); self.assertEqual(o.count(), 1)
            o.close()
            o = sim.Outbox(p, 'us-east-1'); pub = Success()
            s = sim.Sender(o, pub, 100); s.start()
            self.assertEqual(s.finish(2), 0)
            self.assertEqual(pub.seen, [msg]); o.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
