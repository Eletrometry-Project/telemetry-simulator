# Simulador de telemetria Eletrometry

Este programa representa a aquisição de corrente RMS e temperatura nas fases A, B e C de cabines secundárias de uma planta industrial. Produz corrente a cada 1 segundo e temperatura a cada 30 segundos. Pode executar em tempo real, publicar no AWS IoT Core por HTTPS ou gerar histórico local em velocidade acelerada.

A versão 1.0 é uma base funcional e verificável para o projeto. A relação entre carga e aquecimento é modelada, mas os parâmetros ainda não foram calibrados com medições de campo. O perfil industrial é uma hipótese de trabalho, não uma especificação universal para plantas de médio porte. Os exemplos não definem limites legais, de segurança ou de proteção elétrica.

## Arquivos

| Arquivo | Função |
| --- | --- |
| `simulador_eletrometry.py` | Modelo físico, aquisição, arquivos locais e publicação |
| `config_industrial.json` | Cabine de referência de 400 A e TC virtual com faixa de 600 A |
| `config_sct013_100a.json` | Cabine de referência de 63 A e faixa de medição de 100 A |
| `cenarios_exemplo.json` | Sete eventos programados para três cabines |
| `test_simulador.py` | Testes locais sem AWS |
| `requirements.txt` | Dependências para AWS e fusos horários |

Use Python 3.9 ou superior. Os comandos abaixo pressupõem que você está na pasta extraída. No Windows, use `python` ou `py` em lugar de `python3`, conforme sua instalação.

## Primeiro teste local

```bash
python3 -m pip install -r requirements.txt
python3 simulador_eletrometry.py simular --cabines 3 --duracao 120 --modo acelerado
```

Sem `--destino iot`, não há acesso à AWS. A saída vai para uma nova subpasta de `dados/`, identificada pela execução. Cada execução cria sua própria pasta, sem sobrescrever as anteriores.

Sem falhas de comunicação, 120 segundos para três cabines produzem 360 mensagens de corrente e 12 de temperatura, totalizando 1.116 valores individuais. A coleta inclui o instante inicial e exclui o instante final: temperaturas em 0, 30, 60 e 90 segundos.

## Publicar usando a regra IoT e a fila já testadas

O padrão é `eletrometry/demo/cabine-001/corrente` e `eletrometry/demo/cabine-001/temperatura`. A regra existente `SELECT * FROM 'eletrometry/demo/+/+'` aceita esses tópicos. O segmento com a identificação da cabine ocupa o mesmo nível de tópico já usado no teste anterior.

No CloudShell, inicie a sessão do Learner Lab, envie os arquivos e execute:

```bash
python3 simulador_eletrometry.py simular --config config_industrial.json --cabines 3 --duracao 120 --destino iot
```

Por padrão, a região é `us-east-1`. O script usa as credenciais do CloudShell. Localmente, utiliza a cadeia padrão do Boto3: perfil AWS ou variáveis `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` e `AWS_SESSION_TOKEN`. Também aceita `--profile nome`. Na EC2, use o `LabInstanceProfile` permitido pelo laboratório. Nunca coloque credenciais no arquivo de configuração nem envie chaves no chat ou no Git.

O script descobre o endpoint com `iot:DescribeEndpoint`. Se essa ação estiver bloqueada, informe o hostname ATS conhecido com `--endpoint SEU_ENDPOINT.iot.us-east-1.amazonaws.com`. Publicar exige `iot:Publish`. O simulador não cria nem modifica recursos, roles, políticas, certificados ou a regra existente.

O envio usa HTTPS com assinatura AWS, como o teste que já funcionou. Esta versão não implementa MQTT com certificado de dispositivo; isso pode ser acrescentado na classe de transporte posteriormente, preservando o modelo e as mensagens.

O simulador apenas publica. Ele não lê ou apaga mensagens do SQS. Um consumidor separado deve receber, validar, persistir e confirmar essas mensagens.

“Mensagens aceitas pelo IoT” significa resposta de sucesso da API Publish. Isso não comprova que a regra conseguiu entregar ao SQS. Verifique a fila, os erros da regra e, depois, a persistência no consumidor.

## Perfil de corrente e escolha do sensor

O perfil padrão usa corrente nominal de referência de 400 A e faixa de TC de 600 A. São valores explícitos para uma cabine virtual; a seleção do TC físico permanece pendente. Em operação normal, a carga utiliza aproximadamente 62% da nominal no turno e 24% fora dele, antes das pequenas diferenças individuais e da redução de fim de semana.

Os slides citaram SCT-013-000, cuja faixa nominal é 100 A. Esse modelo não deve ser apresentado como capaz de medir 400–600 A diretamente. Para manter a faixa desse sensor, use:

```bash
python3 simulador_eletrometry.py simular --config config_sct013_100a.json --cabines 3 --duracao 120 --destino iot
```

A corrente nominal de 63 A desse perfil também é uma hipótese de configuração, não uma especificação obtida da documentação original. Os dois perfis usam o mesmo comportamento normalizado. Nem a exatidão do conjunto TC/ADC nem a montagem térmica do PT100 foram caracterizadas em campo.

Para definir outro perfil, edite o JSON e ajuste `corrente_nominal_a`, `tc_fundo_escala_a`, `modelo_tc` e os parâmetros de carga. A escala do TC deve cobrir a nominal. Leituras que ultrapassarem a faixa configurada são publicadas como `null` com qualidade `fora_faixa`; o sistema não inventa valores confiáveis fora da faixa nem esconde a condição com um corte silencioso.

## Comportamento físico implementado

Cada cabine possui pequenas diferenças persistentes de carga, distribuição entre fases, ambiente, inércia térmica e ganho dos instrumentos. As fases compartilham a carga principal e apresentam diferenças pequenas em operação normal. Há ciclos de produção suaves, variação temporal correlacionada, troca gradual de turno e redução de carga no fim de semana. Todos esses componentes têm limites.

A temperatura ambiente é uma variável interna do modelo com ciclos diário e sazonal e uma diferença local por cabine. Ela não é enviada como uma sétima medição. A temperatura representa o ponto monitorado na fase, como uma conexão ou região do barramento, e não a temperatura média de toda a cabine.

A cada segundo, o programa atualiza:

1. A carga elétrica e as correntes RMS das fases.
2. A temperatura do ponto monitorado de cada fase.
3. A resposta mais lenta do sensor de temperatura.
4. As leituras, respeitando a frequência de cada grandeza.

Para cada fase, o modelo usa:

```text
q_fase = (I_fase / I_nominal)^2
T_alvo = T_ambiente + K * (0,9 * R_relativa * q_fase + 0,1 * média(q_fases))
T_nova = T_anterior + (T_alvo - T_anterior) * (1 - exp(-1 / tau))
```

`K` é a elevação de temperatura de referência na carga nominal. `tau` é a constante de tempo em segundos. `R_relativa` vale 1 em operação normal e aumenta durante mau contato. A componente de 10% é uma aproximação de influência térmica comum entre fases, não uma geometria física calculada.

O sensor tem sua própria resposta térmica de primeira ordem, seguida de pequeno ruído, erro fixo por fase e arredondamento. Portanto, o aquecimento não salta instantaneamente quando a corrente aumenta e a cabine continua quente após desligar. A condição inicial aproxima uma cabine já operando em regime, não uma partida a frio.

Os valores RMS representam o que o dispositivo enviaria após processar a aquisição elétrica. Não são amostras instantâneas de uma senoide de 60 Hz; não servem para estudar harmônicos, transitórios de curto-circuito, fasores ou qualidade de energia em alta frequência. Não há tensão nem fator de potência medidos, portanto o programa não apresenta kW ou kWh como medições. Não há modelo de arco, incêndio, envelhecimento, disjuntor ou disparo automático de proteção.

## Cenários programados

Sem `--cenarios`, não são injetadas falhas. Um alerta ainda depende dos limites configurados no processador de negócio.

```bash
python3 simulador_eletrometry.py simular --config config_industrial.json --cenarios cenarios_exemplo.json --cabines 3 --duracao 7200 --modo acelerado --inicio 2026-09-28T08:00:00-03:00
```

Esse comando gera duas horas históricas locais sem aguardar duas horas. O horário fixo torna o ensaio repetível e o coloca no turno diurno de uma segunda-feira.

| Cenário | Comportamento |
| --- | --- |
| `sobrecarga` | Correntes se aproximam gradualmente de um múltiplo da nominal; temperaturas respondem com atraso |
| `desequilibrio` | Transfere parte da carga para uma fase e reduz as outras, preservando a soma das magnitudes de corrente neste modelo |
| `mau_contato` | Aumenta o aquecimento local de uma fase, mantendo a corrente do cenário normal |
| `desligamento` | Corrente cai para próximo de zero; temperatura esfria gradualmente; a carga retorna após o evento |
| `sensor_sem_leitura` | Publica `null` e qualidade `sem_leitura` somente no sensor afetado |
| `sensor_congelado` | Repete a última leitura sem declarar a falha; o detector deve reconhecer a anomalia |
| `sem_comunicacao` | Suprime a transmissão durante o evento, enquanto o estado físico continua evoluindo |

`inicio_s` é relativo ao começo da simulação. `duracao_s` usa fim exclusivo. `cabine_id: "*"` aplica o cenário a todas as cabines desta execução. Eventos sobrepostos na mesma cabine são rejeitados porque as interações simultâneas não foram modeladas. O encerramento de mau contato representa reparo/restauração da conexão; o resfriamento continua gradual.

O exemplo usa sobrecarga e desequilíbrio a partir de 10 minutos; mau contato também inicia em 10 minutos e dura 50 minutos. Um teste de dois minutos não alcança esses eventos. Para uma demonstração curta, ajuste o cronograma, mas preserve uma duração suficiente para observar a inércia térmica; não espere uma grande elevação de temperatura em poucos segundos.

Os cenários e seus horários ficam em `cenarios_referencia.json`, separados da telemetria. Não envie esse arquivo como variável de entrada ao detector/modelo preditivo. Os rótulos de causa nunca aparecem nas mensagens dos sensores. Os únicos estados de qualidade enviados são condições de aquisição conhecidas pelo dispositivo. Uma falha silenciosa de congelamento conserva qualidade `ok` se a última leitura era válida.

O evento deliberado `sem_comunicacao` representa perda dos pacotes daquela janela. Falhas reais de rede/API, em contraste, preservam os pacotes na fila local para reenvio. As lacunas de sequência permitem diferenciar a ausência esperada de amostras da frequência natural da temperatura.

## Contrato de mensagens

Cada mensagem agrupa A, B e C de uma única grandeza. Temperatura não é repetida artificialmente nos segundos intermediários.

| Campo | Significado |
| --- | --- |
| `schema_version` | Versão do contrato desta implementação |
| `simulator_version` | Versão do gerador |
| `run_id` | Identidade única da execução |
| `event_id` | Identidade estável do registro, inclusive nos reenvios |
| `executor_id` | Computador/processo gerador identificado pelo grupo |
| `planta_id`, `cabine_id`, `device_id` | Identidades do equipamento virtual |
| `config_id` | Referência aos parâmetros no manifesto |
| `simulated` | Sempre verdadeiro |
| `timestamp` | Horário de coleta simulado em UTC, com `Z` |
| `sequence` | Sequência por execução, cabine e grandeza; inclui lacunas de comunicação |
| `intervalo_s` | 1 para corrente; 30 para temperatura |
| `tipo` | `corrente` ou `temperatura` |
| `unidade` | `A_rms` ou `degC` |
| `valores` | Objeto com A, B, C: número ou `null` |
| `qualidade` | Objeto com A, B, C: `ok`, `sem_leitura` ou `fora_faixa` |

O consumidor deve usar `cabine_id`. Adicione `recebido_em` no lado da ingestão para medir atraso; não substitua o horário original de coleta. Ao atualizar o estado atual, não deixe uma leitura atrasada substituir uma mais recente. Deduplicate pelo `event_id`, porque uma tentativa cujo resultado se perdeu na rede pode ter sido aceita pela AWS.

Não aplique as antigas faixas visuais do Figma automaticamente. Limites de alerta, histerese, duração mínima, abertura/encerramento e correlação são responsabilidade do processador. A corrente nominal do manifesto permite calcular carregamento; não é necessariamente o limite real de proteção.

## Arquivos e memória

Os arquivos de telemetria são JSON Lines comprimidos. Cada linha contém uma mensagem. São separados por data/hora UTC e grandeza, com rotação a cada 20 mil mensagens. Somente os arquivos correntes permanecem abertos; o ano inteiro não é acumulado na RAM.

Dentro de cada execução você encontrará:

- `manifesto.json`: configuração, seed, perfis individuais e argumentos.
- `cenarios_referencia.json`: cronograma conhecido das falhas.
- `telemetria/data=.../hora=.../tipo=.../lote-....jsonl.gz`: medições.
- `resumo.json`: contagem e situação final.
- `pendencias.sqlite3`: somente no modo IoT; fila local de mensagens não aceitas.

Mantenha `manifesto.json` e a referência dos cenários fora da tabela Raw de telemetria. Os arquivos de telemetria podem ser enviados em lote ao S3 Raw no carregamento histórico. O programa não faz upload direto ao S3 nesta versão, para não presumir bucket ou permissões. A cópia local de uma execução ao vivo não deve ser importada como uma segunda fonte sem deduplicação.

Uma cabine, 365 dias e operação contínua produzem 31.536.000 registros de corrente e 1.051.200 de temperatura: 32.587.200 mensagens e 97.761.600 valores individuais. Esse volume exige tempo e espaço reais mesmo no modo acelerado. Comece por horas ou dias e meça armazenamento antes de gerar anos.

## Credenciais expiradas e reenvio

No modo IoT, cada lote de coleta entra na fila SQLite antes de ser publicado. O publicador roda em uma thread separada, limitada por `--taxa-envio`. Ele repete erros transitórios com intervalos crescentes. Erros conhecidos de credenciais/permissão interrompem a execução preservando pendências. Não exclua os arquivos SQLite, WAL ou SHM com o processo aberto.

Após corrigir a sessão, execute:

```bash
python3 simulador_eletrometry.py reenviar --banco dados/ID_DA_EXECUCAO/pendencias.sqlite3 --espera-envio 120
```

Informe `--regiao us-west-2` se essa foi a região original. A região registrada é conferida. Reenvio preserva payload, tópico, sequência, identidade e horário. Nenhuma mensagem aceita anteriormente é reenviada deliberadamente, mas resultados incertos de rede podem gerar duplicações.

Use apenas um processo por arquivo de pendências. Se o limite de 50 mil pendências for atingido, a geração para com erro; não há descarte silencioso nem consumo ilimitado do disco pela fila. O arquivo histórico cresce com a duração, portanto também acompanhe o espaço em disco.

Ao final, o script espera até `--espera-envio` segundos para esvaziar a fila; se ainda houver pendências, termina com código 2 e informa como reenviar. O padrão é 30 segundos. `Ctrl+C` encerra a coleta e fecha os arquivos; os arquivos comprimidos não são uma garantia de recuperação contra desligamento abrupto durante escrita. A fila SQLite é o mecanismo de preservação dos envios pendentes.

## Mais cabines e mais computadores

```bash
python3 simulador_eletrometry.py simular --cabines 3 --primeira-cabine 1 --executor computador-01 --destino iot --duracao 600
python3 simulador_eletrometry.py simular --cabines 3 --primeira-cabine 4 --executor computador-02 --destino iot --duracao 600
```

Execute cada comando no respectivo computador. Isso divide as identidades entre cabines 001–003 e 004–006. Não gere simultaneamente a mesma cabine da mesma planta em dois computadores. Se houver cenários, cada arquivo deve referenciar as cabines atribuídas àquela execução ou `*`.

O padrão limita cada processo a 20 publicações por segundo. Para aumentar a quantidade de cabines, dimensione `--taxa-envio`, a máquina e o consumo de créditos; a capacidade necessária é aproximadamente `cabines * 31/30` mensagens por segundo. O programa rejeita uma taxa configurada menor que essa demanda média, mas isso não garante que CPU/rede consigam atingir a taxa. Acompanhe atraso e pendências nos logs.

Com a mesma seed, configuração, início e identidade de cabine, os valores se repetem mesmo ao dividir as cabines entre máquinas. IDs de execução e eventos mudam em novas execuções para evitar colisões. O estado térmico não é restaurado depois de reiniciar o programa; reenvio recupera mensagens, não continua o modelo físico. Para comparar experimentos, use início explícito e seed fixa.

## Execução contínua e Basic Ingest

O padrão termina após 600 segundos. Execução contínua exige opção explícita:

```bash
python3 simulador_eletrometry.py simular --cabines 3 --destino iot --continuo
```

Encerre com `Ctrl+C` antes de terminar a sessão do Learner Lab. As EC2 consumidoras podem parar enquanto outros serviços continuam ativos.

Basic Ingest é opcional:

```bash
python3 simulador_eletrometry.py simular --cabines 3 --destino iot --duracao 120 --basic-ingest-rule eletrometry_demo_sqs
```

Essa opção publica em `$aws/rules/NOME_REGRA/eletrometry/demo/cabine-NNN/tipo`. Exige que a regra exista e que a identidade tenha permissão de publicação nesse tópico reservado. Não modifica políticas. Diferentemente dos tópicos convencionais, os tópicos de Basic Ingest não podem ser assinados pelo cliente MQTT de teste. Confirme o resultado no destino da regra. A cobrança de mensagens do broker é eliminada nesse caminho, mas regras, ações e destinos continuam sujeitos a custos.

## Verificação

```bash
python3 -m unittest -v test_simulador
```

Os testes verificam frequências, reprodução por seed, limites do perfil normal em 24 horas, atraso térmico, mau contato sem aumento de corrente, conservação da soma no desequilíbrio, desligamento, falhas de aquisição, ausência de comunicação, marcação de fora de faixa, rotação de arquivos e persistência/reenvio após expiração de credenciais simulada. São testes locais: não comprovam desempenho na AWS nem calibração física.

## Fontes técnicas

- [YHDC SCT013 e faixa nominal](https://poweruc.com/product/current-transformers-sct013)
- [Ficha SCT013-000 100 A e 50 mA](https://naylampmechatronics.com/img/cms/000227/SCT013-000-100A-50mA.pdf)
- [Boto3 IoT Data Publish](https://docs.aws.amazon.com/boto3/latest/reference/services/iot-data/client/publish.html)
- [Regra IoT para SQS](https://docs.aws.amazon.com/iot/latest/developerguide/sqs-rule-action.html)
- [Basic Ingest](https://docs.aws.amazon.com/iot/latest/developerguide/iot-basic-ingest.html)

As fontes fundamentam o transporte e a faixa do TC. A carga, a instalação, os coeficientes térmicos e os erros instrumentais deste simulador são hipóteses declaradas a validar com o professor e, quando disponíveis, dados de campo.
