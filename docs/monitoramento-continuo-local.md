# Monitoramento contínuo local — Quidway S3900

Nesta versão, a coleta física
não depende de clicar em "Atualizar leitura". Ao iniciar o Manager no modo
serial, um worker consulta o console em segundo plano; a página apenas lê
`GET /api/dashboard`, sem usar a serial.

## Ativar

1. Mantenha o console físico ligado, com o dispositivo serial definido em
   `local-settings.ps1` (`QM_MODE='serial'`).
2. Inicie como antes: `.venv/bin/python start.py`.
3. Abra `http://127.0.0.1:8787/` e recarregue com Ctrl+Shift+R.
4. O topo mostra "Monitoramento ativo", "Lendo o switch", "CLI aberta · pausado"
   ou o motivo de uma falha.
5. O botão de atualização manual é ocultado somente no modo serial.
6. A aba CLI continua funcionando: com a CLI aberta, o worker não disputa a
   porta console. Ao encerrá-la, o monitoramento é retomado automaticamente.

## Ritmo e fidelidade dos dados

- A tela consulta o cache HTTP a cada ~2,5 segundos quando visível, e ~8
  segundos em segundo plano. Não executa comando serial a cada atualização
  visual.
- A coleta física executa pequenos lotes, com intervalo mínimo de ~4 segundos
  entre tentativas e pausa de ~12 segundos após erro. A duração total depende
  do console serial (9600 baud por padrão, 115200 nos switches atuais), tamanho da resposta e paginação do firmware.
- O rodízio percorre as 28 portas por igual, uma por ciclo. Uma alteração
  guiada pode antecipar sua porta, mas essa prioridade alterna com o rodízio
  normal. Não há mais portas ou VLANs fixas do laboratório na coleta.
- Uma passagem normal exige 28 ciclos: as pausas somam pelo menos 112
  segundos, além do tempo de cada consulta. Isso não garante atualização
  em 112 segundos; serial lenta, respostas grandes, falhas e CLI aberta
  aumentam o intervalo.
- Uma consulta inválida não apaga MACs/VLANs/portas reconhecidos. Cada porta
  contém horários por campo e fica marcada como anterior após 180 segundos
  sem confirmação ou quando a consulta falha. Um campo ausente em resposta
  parcial conserva seu valor e horário anteriores, sem parecer recém-lido.
  Cobertura parcial é exibida mesmo quando a última consulta foi válida.
- Inventário incompleto não apaga uma VLAN já confirmada. A ausência é
  verificada individualmente; só uma resposta explícita de inexistência
  autoriza remover a VLAN do painel. Associações de portas respeitam a
  precedência temporal das observações.
- O S3900 reporta alguns indicadores como médias de **300 segundos**, por
  isso o painel é de atualização contínua, não telemetria instantânea.
- O worker usa somente comandos `display` de leitura. As configurações do
  switch não são modificadas.
- As observações ainda são mantidas **na memória do servidor** e se perdem
  ao parar o processo. Histórico persistente de testes requer implementação
  separada.

## Validação de código

```bash
cd ~/Projetos/switch-manager
.venv/bin/python -m unittest discover -s tests -q
node --check static/app.js
node --test tests/frontend_reliability.cjs
```

A branch de referência é `feature/s3900-virtual`; as correções de 17/11 estão apenas nesta cópia local, sem commit/push/PR. Configurações locais e credenciais não são incluídas na entrega. O registro opcional `lab_capture.py` grava telemetria de laboratório, não pacotes Ethernet/PCAP.

## Recuperação e registro de laboratório

Uma falha antes da identificação não cria um cache válido. Ao recuperar o console, o monitor repete `display version` e só inicia outras consultas após reconhecer o perfil. Respostas com outra interface ou múltiplas interfaces são rejeitadas. O rodízio de detalhes de VLAN alterna prioridades e consultas normais; prioridades também giram entre os IDs, evitando monopolização por inventários incompletos.

Quando ativado pelo marcador de laboratório, o JSONL inclui horários e flags de validade por campo, além de `collection_stale` para o estado global da coleta e `stale`/`coverage_partial` para o conteúdo. Um `observed_at` da porta não equivale a uma amostra nova de todas as suas taxas. Registros antigos sem metadados por campo não permitem recuperar retroativamente esses horários; não foram reescritos.
