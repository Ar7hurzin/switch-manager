# Quidway Manager

Painel local em português para observar e configurar, de forma controlada, um switch Huawei Quidway S3900. A versão atual oferece demonstração, simulador S3900, leitura SSHv2 e monitoramento contínuo pelo console serial. O perfil físico é Quidway 3900-SI 28-Port, VRP 3.10(CN) RELEASE 0002. As correções locais são testadas com respostas simuladas e não comprovam compatibilidade física de novos comandos de escrita nesse release.

## O que funciona

- Painel web com visão geral, mapa conceitual de portas, VLANs e diagnóstico.
- Áreas separadas pela navegação principal: Dashboard em `/`, Sistema em `/sistema`, Monitoramento em `/monitoramento`, Configuração em `/configuracao` e CLI em `/cli`. As páginas reutilizam a mesma estrutura visual, mas mostram conteúdos diferentes.
- Login SSO com sessões de usuários ativos no Dashboard. A lista usa atividade recente e expira o indicador após cinco minutos sem requisições; o painel mostra MACs aprendidos quando a resposta é reconhecida, com horário e indicação de dados antigos; MACs não são uma contagem de pessoas ou prova de conexão ativa.
- Demonstração claramente identificada; os valores de exemplo não vêm de um equipamento.
- Modo SSH configurável com validação estrita da chave conhecida. O único conjunto permitido é `display version` e `display vlan`.
- **Detecção automática do switch pelo console:** ao conectar o cabo, o painel identifica switches Huawei antigos (VRP 3), Huawei-3Com e 3Com (Comware 3), descobre as portas e se adapta sozinho. O S3900-SI tem configuração guiada validada. Os demais VRP 3 / Comware 3 recebem configuração guiada adaptada, e uma operação que o switch recusar passa a ser feita pela CLI naquele modelo. Sistemas incompatíveis usam só a CLI. Veja [switches suportados](docs/switches-suportados.md).
- Modo serial com consultas fixas de identificação, inventário/detalhes de VLAN, interfaces e tabela MAC. O monitor em segundo plano respeita a exclusividade da CLI; feche outros terminais que usem o mesmo console. Veja [ritmo e fidelidade dos dados](docs/monitoramento-continuo-local.md).
- Área Configuração com prévia de comandos para VLANs, portas access/trunk/hybrid, estado/descrição de porta, nome do switch, IP de gestão, backup e salvamento.
- Aplicação de mudanças por série serial usa operações tipadas, prévia de cada plano e verificação do modelo/release antes de enviar. Depois da prévia, um clique explícito aplica a operação. Gravações ficam desabilitadas por padrão por `QM_ENABLE_CONFIG_WRITES=false`.
- Verificação estrita de chave SSH conhecida. Chave nova ou alterada é rejeitada.
- Uma credencial por vez: senha ou chave privada. A senha não é solicitada nem gravada pelo painel.
- Serviço web preso a `127.0.0.1`; cabeçalhos impedem cache do diagnóstico no navegador.

## O que ainda depende de conhecer o switch

As gravações são baseadas no manual de comandos da série S3900 Release 1510. O aparelho observado no terminal usa VRP 3.10(CN) RELEASE 0002, portanto a coleta serial pelo Manager e a sintaxe de escrita ainda precisam ser verificadas nesse release; gravações permanecem bloqueadas por padrão. Recursos avançados como AAA/SSH, STP, QoS, ACLs, roteamento, DHCP, SNMP e agregação ainda não têm interface de escrita. O mapa de portas e a tabela de taxas são preenchidos por consultas reconhecidas. Captura Ethernet/PCAP e gráficos históricos no estilo Wireshark ainda não estão implementados. O SSH observado informou versão 1.5; não habilitamos suporte a protocolo legado.

## Iniciar

Para os comandos de uso diário e instalação em cada sistema, consulte [Comandos rápidos](docs/comandos-rapidos.md).

A interface **Quidway Light** usa tema claro e o mesmo endereço local. Há um inicializador Python compartilhado para Windows/Linux; consulte [instalação nativa e Docker](docs/instalacao-docker.md) para comandos de instalação, autenticação, Docker Compose e limitações.


Na primeira execução, abra a pasta `QuidwayManager` no VS Code. No terminal PowerShell dessa pasta, configure o login SSO:

```powershell
.\configure-login.ps1
```

O login é feito por **SSO (OpenID Connect) com autenticação de dois fatores** no provedor da organização (Entra ID, Keycloak, Authentik…). Nenhuma senha de usuário é pedida ou gravada. O assistente pede o issuer, o client ID, os e-mails autorizados e a redirect URI a cadastrar no provedor. O arquivo `local-settings.ps1`, ignorado pelo Git, recebe só essas informações e uma chave de sessão aleatória. Veja [login SSO com 2FA](docs/login-sso.md). Depois inicie o app:

```powershell
.\start.ps1
```

O script prepara as bibliotecas na primeira execução e inicia o servidor. Abra `http://127.0.0.1:8787` no navegador e clique em **Entrar com SSO**. A sessão expira em oito horas; use **Sair** para revogá-la imediatamente no servidor. Reiniciar o servidor ou reconfigurar o login exige entrar novamente. O registro de autorização é separado da presença recente de cinco minutos e requer a execução em um único processo, como no inicializador fornecido. Deixe a janela do terminal aberta enquanto usar o app; pressione `Ctrl+C` para encerrá-lo. Se o app já estiver rodando, o script verifica se a versão ativa exige login antes de reutilizá-la.

## Acesso privado remoto pelo Tailscale

O Tailscale Serve coloca o painel HTTPS **dentro da tailnet**, sem abrir uma porta no roteador e sem usar Funnel. As regras de acesso da tailnet continuam valendo. O computador que hospeda o app precisa permanecer ligado, conectado ao Tailscale, e com o app iniciado.

1. Inicie o Quidway Manager com `.\start.ps1`.
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

Inicie com `.\start.ps1` no Windows ou `.venv/bin/python start.py` no Fedora. No modo serial a coleta é automática; a página consulta o cache e oculta o botão **Atualizar leitura**. O primeiro uso instala PySerial pelo `requirements.txt`. O Tailscale pode compartilhar o painel com a tailnet, mas o computador hospedeiro precisa ficar ligado e fisicamente conectado ao switch. A porta serial não é transportada para outro computador pelo Tailscale.

## Área Configuração

A tela cria prévias para operações de VLAN, modo/VLAN de portas, descrição e estado administrativo, nome do switch, IP de gestão e rotas estáticas IPv4. O painel Cobertura indica quais grupos têm controles e quais aguardam validação. Cada prévia lista os comandos e riscos; um clique explícito aplica o plano. A VLAN 1 não pode ser removida pela interface. Backup é opcional e exportado sob demanda; o arquivo bruto pode conter segredos. Salvar no switch continua sendo uma ação separada com sua própria prévia.

As mudanças entram primeiro na configuração em execução do switch; salvar é uma etapa separada. O envio permanece bloqueado enquanto `QM_ENABLE_CONFIG_WRITES=false`. Para habilitar, o operador precisa revisar o código e validar as operações com o console e o release instalado; a versão 0002 não foi usada para testar comandos de escrita. O Manager ainda não cobre todas as funções do switch, então não substitui completamente a CLI nesta etapa. A área não oferece comandos livres e não importa arquivo arbitrário para executar.

## Aba CLI

A rota `/cli` oferece um terminal interativo do console serial. Clique em **Conectar ao console** para abrir a COM configurada; depois digite uma única linha e pressione Enter ou **Enviar**. O app não envia comandos de configuração ao conectar; envia apenas uma linha vazia para acordar o prompt. Cada comando digitado é transmitido imediatamente. Saídas com prompts `More` são paginadas automaticamente; confirme a resposta exibida antes de prosseguir. **Enviar Enter** atende prompts que aguardam uma linha vazia.

A aba CLI é um terminal de uso direto, sem prévia, backup automático ou rollback. A conexão CLI é desabilitada por padrão (`QM_ENABLE_CLI=false`). Após validação e autorização explícita, `QM_ENABLE_CLI=true` permite acesso ao terminal; ele não filtra comandos de configuração, mesmo se a escrita guiada permanecer bloqueada. Use a área guiada para operações tipadas quando ela cobrir a tarefa. Comandos e respostas não são persistidos pela aplicação, mas podem ficar visíveis na tela do navegador até limpar a saída ou fechar a aba; a resposta do switch pode conter credenciais/hashes. Entradas são ocultadas quando o Manager detecta prompts/comandos com termos comuns de senha, mas essa detecção não cobre todos os formatos. A sessão serial é exclusiva, fecha explicitamente ou após 20 minutos sem atividade. A saída da página tenta encerrá-la; se não houver confirmação, o token é preservado para recuperação e nova tentativa. O logout também tenta fechar a CLI pertencente à sessão. Enquanto ativa, monitoramento, backup e aplicação guiada são bloqueados para evitar disputa pela porta. O app não habilita SSH 1.5; o terminal integrado usa somente a porta console local e pode ser acessado remotamente pelo painel privado na tailnet.

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

- A área de configuração guiada não aceita comandos livres; a CLI tem habilitação separada. As operações tipadas de configuração só podem ser enviadas quando `QM_ENABLE_CONFIG_WRITES=true`; por padrão, a gravação permanece bloqueada.
- Todas as páginas e APIs de operação exigem login SSO (OpenID Connect) com 2FA comprovado no token e e-mail na allowlist; nenhuma senha é armazenada. A sessão usa cookie HttpOnly, SameSite=Lax, proteção CSRF e expira após oito horas. O login limita tentativas por endereço de origem; sob proxy os usuários podem compartilhar esse limite. Quando o painel é aberto por HTTPS (por exemplo, pelo Tailscale Serve em `*.ts.net`), o cookie de sessão recebe `Secure` automaticamente; o acesso HTTP local em 127.0.0.1 continua funcionando. `QM_HTTPS_ONLY=true` força `Secure` em todos os acessos (não habilite se usar o HTTP local). O endpoint de saúde fica público apenas para o script local verificar se o serviço iniciou.
- Mantenha o servidor nativo em `127.0.0.1`; Docker usa porta interna publicada somente no loopback do host. O acesso remoto privado é intermediado pelo Tailscale Serve.
- As regras de acesso da tailnet continuam sendo uma camada necessária; limite o acesso aos seus dispositivos/usuários autorizados.
- Não crie encaminhamento de porta no roteador nem publique o painel com Tailscale Funnel.
- Não considere a compatibilidade, backup, monitoramento ou recuperação confirmados até validar o equipamento real.

## Documentação

- [Instalação Windows, Fedora e Docker (passo a passo)](docs/instalacao-docker.md)

- [Como conectar e usar (TXT)](docs/como-conectar-e-usar.txt)
- [Módulo de configuração pela interface](docs/configuracao-interface.md)
- [Guia técnico (PDF)](docs/guia-tecnico.pdf) · [DOCX editável](docs/guia-tecnico.docx)
- [Guia de planejamento (PDF)](docs/guia-de-planejamento.pdf) · [DOCX editável](docs/guia-de-planejamento.docx)
- [Mockups conceituais](docs/mockups-conceituais.png)



## Arquivo histórico

O diretório `archives/` guarda o ZIP de código exportado anteriormente. Ele pode estar defasado em relação à versão atual do projeto; o conteúdo atualizado está nos arquivos versionados na raiz e em `static/` e `docs/`.

## Correções e validação local

As sete falhas da revisão de 16/11 foram tratadas nesta cópia local. Leia [correções e critérios de validação](docs/correcoes-2026-11.md) antes de habilitar escrita física. As versões exercitadas estão em `constraints.txt`, aplicado pelo `requirements.txt`; a compatibilidade entre plataformas continua sendo verificada pela matriz de testes. Não é necessário reinstalar o ambiente a cada alteração de código.

```bash
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
node --test tests/frontend*.cjs
.venv/bin/python -m pip check
```

A segunda rodada de revisão também foi corrigida: recuperação da identificação inicial, listas multilinha, fila de VLANs, identificação de interface, validade dos registros de laboratório e concorrência da CLI/prévias. Veja [segunda rodada de correções](docs/segunda-correcao-2026-11-17.md). Prévias trunk/hybrid agora consultam o modo atual pelo console e exigem que a CLI esteja fechada.
