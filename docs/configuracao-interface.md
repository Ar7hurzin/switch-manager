# Módulo de configuração do Switch Manager

## Escopo implementado

A área **Configuração** cria prévias tipadas para estas operações:

- VLAN: criar com nome, renomear e excluir VLANs (a VLAN 1 é protegida).
- Portas: access com VLAN, trunk com lista de VLANs permitidas/PVID, hybrid com listas tagged/untagged/PVID, descrição e estado administrativo.
- Sistema: alterar o nome do switch e o IPv4 de uma interface VLAN.
- Roteamento: adicionar ou excluir rota estática IPv4, com destino/prefixo/próximo salto validados e prévia explícita.
- Arquivos: exportar a configuração atual sob demanda e salvar separadamente as mudanças em execução no switch.

O app não recebe comandos livres do navegador. Cada ação tem validação de entradas, prévia dos comandos, avisos e token de uso único com validade de cinco minutos. Depois de revisar a prévia, um clique envia a operação; o backup é opcional e recomendado, mas não bloqueia o envio. A checagem do modelo/release continua obrigatória. A alteração do IP avisa que o gerenciamento de rede pode cair. O app usa a porta serial local.

### Terminal CLI integrado

A aba **CLI** (`/cli`) abre uma sessão serial direta para digitar comandos do VRP um por vez. Conectar não envia comandos de configuração; apenas envia Enter vazio para acordar o prompt. Comandos são transmitidos imediatamente, sem prévia, backup automático ou rollback. A CLI ignora `QM_ENABLE_CONFIG_WRITES=false`, que bloqueia apenas as ações guiadas; pela CLI, qualquer comando de configuração enviado é executado. Há botão **Enviar Enter** para prompts interativos, limite de uma linha ASCII com 512 caracteres, resposta limitada a 256 KB e encerramento após 20 minutos sem atividade. O token da sessão fica somente na memória desta aba e em `sessionStorage`; a saída não é salva no servidor. A saída permanece visualmente na tela e pode conter dados sensíveis. Entradas são ocultadas quando o Manager detecta termos de senha conhecidos, mas isso não substitui cuidado com outros segredos. Monitoramento e as operações guiadas que usam serial ficam bloqueados enquanto a CLI mantém a porta ocupada.

Esse modo atende comandos não cobertos por formulários, mas exige que a pessoa saiba o comando correto para o modelo/release conectado. Não foi testado com o switch nesta implementação; confirme a sessão pelo console e use um comando de leitura como `display version` antes de configurar. O botão Enviar é uma ação real no equipamento.

## Perfil detectado

O equipamento conectado respondeu como Quidway 3900-SI 28-Port, VRP 3.10(CN) RELEASE 0002. O conector usa a numeração documentada para o S3900 de 24 portas FE e 4 GE: portas 1–24 mapeiam para `Ethernet1/0/1`–`Ethernet1/0/24`, e 25–28 para `GigabitEthernet1/1/1`–`GigabitEthernet1/1/4`.

As formas dos comandos se baseiam no Huawei Quidway S3900 Series Command Manual, Release 1510. O manual consultado é de um release diferente do firmware instalado, então as operações de escrita permanecem bloqueadas por padrão até que sejam validadas no release 0002. O usuário executou as consultas `display version`/`display vlan` pelo terminal serial; a leitura pelo coletor do Manager e todos os comandos de alteração ainda precisam ser verificados no próprio app.

## Como revisar uma operação

1. Mantenha o cabo console ligado e feche Tera Term/MobaXterm.
2. No painel, abra **Configuração** e preencha uma ação.
3. Use **Pré-visualizar**. Leia cada comando e os avisos; a prévia não altera o switch.
4. Opcionalmente, baixe um backup antes de aplicar. O arquivo bruto pode incluir credenciais ou hashes; armazene-o em local privado.
5. Antes de habilitar gravações, confirme a sintaxe do firmware instalado. O módulo consulta `display version` e `display vlan` antes de enviar qualquer plano e recusa um equipamento fora do perfil conhecido.
6. Revise a prévia e clique em **Aplicar no switch**. Cada plano é usado uma vez e expira em cinco minutos.
7. As mudanças entram em execução imediatamente. A ação **Salvar configuração** é separada e grava as mudanças no arquivo de inicialização; faça novo backup e revise antes de salvar.

## Bloqueio de gravações

O modo serial do projeto mantém `QM_ENABLE_CONFIG_WRITES=false` em `local-settings.ps1`. Com esse valor, o app permite preparar e revisar planos, mas a API recusa o envio ao switch. A configuração não foi habilitada nesta implementação.

Depois de validar o comando em console local e revisar o código, o operador pode habilitar a variável em `local-settings.ps1`:

```powershell
$env:QM_ENABLE_CONFIG_WRITES = 'true'
```

Reinicie o Switch Manager para carregar a variável. A habilitação não executa mudanças por si só; cada ação ainda exige prévia, backup, frase e clique em **Aplicar no switch**.

## Limites atuais

O módulo ainda não cobre todas as funções disponíveis na família S3900. A interface ainda não inclui configuração guiada para agregação de links, STP/MSTP, espelhamento, IGMP/Voice VLAN, DHCP relay, ACL/QoS, AAA/SSH, SNMP, NTP, syslog ou restauração de backup. O painel de escopo na tela Configuração lista esses grupos e marca que dependem de validação de firmware/modelo. Recursos EI não devem ser presumidos no S3900-SI. A capacidade de VLAN do modelo limita a quantidade de VLANs ativas, enquanto os IDs podem estar no intervalo 1–4094; confirmar a capacidade efetiva do submodelo e release.

A versão instalada é antiga e SSH informa versão 1.5; as alterações continuam disponíveis pelo console serial somente após validação do release. A aplicação não faz rollback automático se o switch rejeitar um comando após aceitar comandos anteriores; nesse caso, reconecte pelo console, revise o estado e use o backup manual conforme apropriado.

## Referência de comandos

As formas de VLAN, interface, endereço IP de VLAN, `shutdown`, `sysname`, rota estática e `save` foram cotejadas com o manual S3900 Release 1510. A família/modelo precisa ser cotejada com o [Command Manual da série Huawei Quidway S3900](https://www.manualslib.com/manual/1144604/Huawei-Quidway-S3900-Series.html) e com o release 0002 antes de habilitar gravações. Esse manual é de outro release; prévia no Manager não é evidência de compatibilidade.

## Correções de novembro de 2026

Trunk e hybrid representam a lista completa desejada; a prévia inclui comandos explícitos de remoção antes de adicionar a lista nova. Essa substituição pode interromper o tráfego durante a execução e não é atômica. A aplicação verifica o resultado por consultas; rejeição, estado divergente ou leitura inconclusiva não são reportados como sucesso. Veja [notas de validação](correcoes-2026-11.md).

Sair revoga a autorização da sessão no servidor, inclusive uma cópia anterior do cookie. Reiniciar o servidor exige novo login. Presença recente no Dashboard não determina a duração da autorização. O inicializador usa um único processo; múltiplos processos independentes não compartilham estado serial/sessões.

## Prévias dependentes do estado atual

Para trunk/hybrid, gerar uma prévia exige ler o modo da porta pelo console, com exclusividade e identificação do perfil. A prévia inclui a passagem por access somente quando a troca entre trunk e hybrid precisa dela. Se o modo já for o desejado, não há etapa access intermediária. Se o modo mudar após a prévia, a aplicação é bloqueada antes de comandos de configuração e solicita nova prévia. A leitura de verificação considera listas multilinha e rejeita ambiguidades.

Durante Aplicar, os formulários ficam bloqueados. Respostas atrasadas de uma prévia antiga são descartadas. Na CLI, botão e teclado compartilham o mesmo bloqueio de envio; nenhuma repetição automática é feita após falha de rede.
