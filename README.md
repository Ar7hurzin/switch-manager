# Switch Manager

Painel local em português para observar e configurar, de forma controlada, um switch Huawei Switch S3900. A versão atual oferece demonstração, leitura SSHv2 e conexão serial. O usuário confirmou pelo terminal serial o Switch 3900-SI 28-Port, VRP 3.10(CN), com COM9 a 9600 baud e executou consultas de leitura; a coleta serial pelo Manager e a compatibilidade dos comandos de alteração ainda não foram validadas neste release.

## O que funciona

- Painel web com visão geral, mapa conceitual de portas, VLANs e diagnóstico.
- Áreas separadas pela navegação principal: Dashboard em `/`, Sistema em `/sistema`, Monitoramento em `/monitoramento`, Configuração em `/configuracao` e CLI em `/cli`. As páginas reutilizam a mesma estrutura visual, mas mostram conteúdos diferentes.
- Login local com sessões de usuários ativos no Dashboard. A lista usa atividade recente e expira o indicador após cinco minutos sem requisições; a contagem de dispositivos do switch fica indisponível até validar a leitura MAC/porta neste firmware.
- Demonstração claramente identificada; os valores de exemplo não vêm de um equipamento.
- Modo SSH configurável com validação estrita da chave conhecida. O único conjunto permitido é `display version` e `display vlan`.
- Modo serial local implementado para enviar somente `display version` e `display vlan`; requer fechar Tera Term/MobaXterm antes da coleta. O console serial e essas respostas foram observados no terminal do usuário, não verificados pelo Manager nesta sessão.
- Área Configuração com prévia de comandos para VLANs, portas access/trunk/hybrid, estado/descrição de porta, nome do switch, IP de gestão, backup e salvamento.
- Aplicação de mudanças por série serial usa operações tipadas, prévia de cada plano e verificação do modelo/release antes de enviar. Depois da prévia, um clique explícito aplica a operação. Gravações ficam desabilitadas por padrão por `QM_ENABLE_CONFIG_WRITES=false`.
- Verificação estrita de chave SSH conhecida. Chave nova ou alterada é rejeitada.
- Uma credencial por vez: senha ou chave privada. A senha não é solicitada nem gravada pelo painel.
- Serviço web preso a `127.0.0.1`; cabeçalhos impedem cache do diagnóstico no navegador.

## O que ainda depende de conhecer o switch

As gravações são baseadas no manual de comandos da série S3900 Release 1510. O aparelho observado no terminal usa VRP 3.10(CN) RELEASE 0002, portanto a coleta serial pelo Manager e a sintaxe de escrita ainda precisam ser verificadas nesse release; gravações permanecem bloqueadas por padrão. Recursos avançados como AAA/SSH, STP, QoS, ACLs, roteamento, DHCP, SNMP e agregação ainda não têm interface de escrita. O mapa de portas e gráficos também não são preenchidos automaticamente. O SSH observado informou versão 1.5; não habilitamos suporte a protocolo legado.

## Iniciar

Na primeira execução, abra a pasta `SwitchManager` no VS Code. No terminal PowerShell dessa pasta, configure o login local:

```powershell
.\configure-login.ps1
```

Escolha um usuário e uma senha com pelo menos 12 caracteres. A senha é digitada sem aparecer na tela; o arquivo `local-settings.ps1` recebe somente o hash PBKDF2, uma chave de sessão aleatória e o nome de usuário. Esse arquivo está ignorado pelo Git. Depois inicie o app:

```powershell
.\start.ps1
```

O script prepara as bibliotecas na primeira execução e inicia o servidor. Abra `http://127.0.0.1:8787` no navegador e entre com a conta que criou. A sessão expira em oito horas; use **Sair** para encerrá-la antes. Deixe a janela do terminal aberta enquanto usar o app; pressione `Ctrl+C` para encerrá-lo. Se o app já estiver rodando, o script verifica se a versão ativa exige login antes de reutilizá-la.

## Acesso privado remoto pelo Tailscale

O Tailscale Serve coloca o painel HTTPS **dentro da tailnet**, sem abrir uma porta no roteador e sem usar Funnel. As regras de acesso da tailnet continuam valendo. O computador que hospeda o app precisa permanecer ligado, conectado ao Tailscale, e com o app iniciado.

1. Inicie o Switch Manager com `.\start.ps1`.
2. Execute `.\enable-tailnet.ps1` no PowerShell.
3. Se o Tailscale informar que Serve ainda não está habilitado, abra o link oficial mostrado no terminal, entre na conta se necessário e aprove a ativação para a tailnet. Depois execute `.\enable-tailnet.ps1` novamente.
4. O terminal mostrará o endereço HTTPS privado. Em outro dispositivo, conecte-se à mesma tailnet e abra esse endereço.

O comando de configuração verifica se o app responde localmente e não substitui uma configuração Tailscale Serve já existente. Para conferir o compartilhamento, use `tailscale serve status`. Para desligar o compartilhamento, use `tailscale serve off`.

Consulte a documentação oficial de [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve). Serve compartilha o serviço com dispositivos autorizados pelas regras da tailnet; confirme que essas regras correspondem às pessoas que devem acessar o painel.

## Leitura pelo console serial

Mantenha o computador conectado ao switch pelo cabo console. Confirme a porta COM no Gerenciador de Dispositivos e configure a velocidade igual à usada pelo terminal. O exemplo inicia com COM9 e 9600 baud, valores que ainda precisam ser conferidos. Feche Tera Term/MobaXterm para liberar a porta, copie `local-settings.example.ps1` para `local-settings.ps1` e habilite:

```powershell
$env:QM_MODE = 'serial'
$env:QM_SERIAL_PORT = 'COM9'
$env:QM_SERIAL_BAUD = '9600'
```

Inicie com `.\start.ps1` e use **Atualizar leitura**. O primeiro uso instala PySerial pelo `requirements.txt`. O Tailscale pode compartilhar o painel com a tailnet, mas o computador hospedeiro precisa ficar ligado e fisicamente conectado ao switch. A porta serial não é transportada para outro computador pelo Tailscale.

## Área Configuração

A tela cria prévias para operações de VLAN, modo/VLAN de portas, descrição e estado administrativo, nome do switch, IP de gestão e rotas estáticas IPv4. O painel Cobertura indica quais grupos têm controles e quais aguardam validação. Cada prévia lista os comandos e riscos; um clique explícito aplica o plano. A VLAN 1 não pode ser removida pela interface. Backup é opcional e exportado sob demanda; o arquivo bruto pode conter segredos. Salvar no switch continua sendo uma ação separada com sua própria prévia.

As mudanças entram primeiro na configuração em execução do switch; salvar é uma etapa separada. O envio permanece bloqueado enquanto `QM_ENABLE_CONFIG_WRITES=false`. Para habilitar, o operador precisa revisar o código e validar as operações com o console e o release instalado; a versão 0002 não foi usada para testar comandos de escrita. O Manager ainda não cobre todas as funções do switch, então não substitui completamente a CLI nesta etapa. A área não oferece comandos livres e não importa arquivo arbitrário para executar.

## Aba CLI

A rota `/cli` oferece um terminal interativo do console serial. Clique em **Conectar ao console** para abrir a COM configurada; depois digite uma única linha e pressione Enter ou **Enviar**. O app não envia comandos de configuração ao conectar; envia apenas uma linha vazia para acordar o prompt. Cada comando digitado é transmitido imediatamente. Saídas com prompts `More` são paginadas automaticamente; confirme a resposta exibida antes de prosseguir. **Enviar Enter** atende prompts que aguardam uma linha vazia.

A aba CLI é um terminal de uso direto, sem prévia, backup automático ou rollback. Ela ignora `QM_ENABLE_CONFIG_WRITES=false`, que bloqueia apenas a área guiada; portanto, comandos configuradores enviados por CLI são executados mesmo com essa variável desligada. Use a área guiada para operações tipadas quando ela cobrir a tarefa. Comandos e respostas não são persistidos pela aplicação, mas podem ficar visíveis na tela do navegador até limpar a saída ou fechar a aba; a resposta do switch pode conter credenciais/hashes. Entradas são ocultadas quando o Manager detecta prompts/comandos com termos comuns de senha, mas essa detecção não cobre todos os formatos. A sessão serial é exclusiva, fecha explicitamente, no encerramento da aba ou após 20 minutos sem atividade. Enquanto ativa, monitoramento, backup e aplicação guiada são bloqueados para evitar disputa pela porta. O app não habilita SSH 1.5; o terminal integrado usa somente a porta console local e pode ser acessado remotamente pelo painel privado na tailnet.

## Preparar SSHv2 quando o switch estiver disponível

Primeiro, identifique modelo, versão do firmware, IP de gestão e usuário de leitura. Obtenha a chave pública SSH do switch por um canal local confiável e compare sua impressão digital presencialmente. Só depois inclua a chave conferida no arquivo `known_hosts`.

Copie `local-settings.example.ps1` para `local-settings.ps1` e preencha as variáveis de conexão. O arquivo local é ignorado pelo Git. Prefira uma chave privada protegida pelas permissões do Windows; se usar senha, defina `QM_SSH_PASSWORD` somente na sessão PowerShell atual em vez de salvá-la em arquivo.

Exemplo das variáveis necessárias:

```powershell
$env:QM_MODE = 'real'
$env:QM_SWITCH_HOST = 'IP_DE_GESTAO'
$env:QM_SWITCH_PORT = '22'
$env:QM_SWITCH_USER = 'USUARIO_DE_LEITURA'
$env:QM_KNOWN_HOSTS = 'C:\caminho\protegido\known_hosts'
$env:QM_SSH_KEY_FILE = 'C:\caminho\protegido\chave_privada'
```

Feche e inicie novamente o app após alterar configurações. No modo SSH, use **Atualizar leitura** para executar apenas as duas consultas permitidas. Uma falha de negociação SSH não reduz os algoritmos de segurança do computador. O switch observado informou SSH 1.5; só use esse modo se a compatibilidade com SSHv2 tiver sido confirmada para esse firmware.

## Limites de segurança

- A interface não aceita comandos livres. As operações tipadas de configuração só podem ser enviadas quando `QM_ENABLE_CONFIG_WRITES=true`; por padrão, a gravação permanece bloqueada.
- Todas as páginas e APIs de operação exigem login local. A sessão usa cookie HTTP-only, proteção CSRF e expira após oito horas. O endpoint de saúde fica público apenas para o script local verificar se o serviço iniciou.
- Mantenha o servidor em `127.0.0.1`; o acesso remoto é intermediado pelo Tailscale Serve.
- As regras de acesso da tailnet continuam sendo uma camada necessária; limite o acesso aos seus dispositivos/usuários autorizados.
- Não crie encaminhamento de porta no roteador nem publique o painel com Tailscale Funnel.
- Não considere a compatibilidade, backup, monitoramento ou recuperação confirmados até validar o equipamento real.

## Documentação

- [Como conectar e usar (TXT)](docs/como-conectar-e-usar.txt)
- [Módulo de configuração pela interface](docs/configuracao-interface.md)
- [Guia técnico (PDF)](docs/guia-tecnico.pdf) · [DOCX editável](docs/guia-tecnico.docx)
- [Guia de planejamento (PDF)](docs/guia-de-planejamento.pdf) · [DOCX editável](docs/guia-de-planejamento.docx)
- [Mockups conceituais](docs/mockups-conceituais.png)



## Arquivo histórico

O diretório `archives/` guarda o ZIP de código exportado anteriormente. Ele pode estar defasado em relação à versão atual do projeto; o conteúdo atualizado está nos arquivos versionados na raiz e em `static/` e `docs/`.
