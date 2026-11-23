# Login SSO com 2FA (OpenID Connect)

O Switch Manager não tem mais login local. Quem entra se autentica no provedor de identidade da organização (Microsoft Entra ID, Keycloak, Authentik, Google…) com autenticação de dois fatores. O aplicativo **nunca recebe, guarda ou calcula hash de senha de usuário**. Ele só confere um ID token assinado pelo provedor.

## Como funciona

1. **Entrar com SSO** envia o navegador ao provedor (Authorization Code + PKCE S256, com `state` e `nonce` de uso único, válidos por 10 minutos).
2. O provedor autentica a pessoa, com senha e 2FA, e devolve um código para `/auth/callback`.
3. O servidor troca o código pelo ID token e confere:
   - a assinatura, pelas chaves públicas (JWKS) do provedor. Só são aceitos algoritmos assimétricos; `none` e HS256 são recusados;
   - `iss`, `aud`/`azp`, `exp`, `iat` e `nonce`;
   - `email_verified = true` e o e-mail na allowlist `QM_OIDC_ALLOWED_EMAILS`;
   - **2FA**: o claim `amr` contém um dos valores de `QM_OIDC_MFA_AMR` (padrão `mfa,otp,totp,hwk,fido,webauthn`) **ou** o `acr` está em `QM_OIDC_ACCEPTED_ACR`. Se o token não prova 2FA, o login é recusado.
4. A sessão continua como antes: cookie HttpOnly, CSRF, 8 horas, revogável em **Sair**. Mudar o provedor, o client ID ou a allowlist revoga todas as sessões.

## Quem pode entrar: tela de Administração

- **Administrador único:** `QM_OIDC_ADMIN_EMAIL`, definido no `configure`. Sem essa variável, o administrador é o primeiro e-mail de `QM_OIDC_ALLOWED_EMAILS`. Ele sempre entra, e só ele vê o botão **Administração** (`/admin`).
- **Pedidos de acesso:** quem entra pelo SSO sem estar liberado recebe "pedido enviado ao administrador" e não acessa nada. O pedido aparece em **Administração → Pedidos de acesso**, com nome e e-mail confirmados pelo provedor, e um contador vermelho no botão Administração. Use **Aprovar** ou **Recusar**. O pedido só é registrado depois de toda a validação do login (assinatura, e-mail verificado e 2FA, quando exigido). Quem é recusado só pode pedir de novo após 24 horas, e há um limite de 200 pedidos pendentes.
- **Liberar pessoas:** em **Administração**, adicione e-mails (`pessoa@ufu.br`) ou domínios inteiros (`@ufu.br`). A mudança vale na hora, sem reiniciar. Remover alguém encerra o acesso imediatamente, mesmo com o painel aberto.
- Todos os liberados têm o mesmo acesso ao painel. Só o administrador gerencia a lista.
- **Google e domínios:** `@ufu.br` libera só contas do **Google Workspace** daquela organização (claim `hd`). Contas Gmail pessoais, mesmo com um e-mail @ufu.br verificado, precisam ser adicionadas uma a uma. Domínios públicos (`@gmail.com`, `@hotmail.com`…) são recusados.
- A lista fica em `instance/access-list.json`, ao lado do `local-settings.ps1` (no Docker, `/data/instance/`), com permissão 600. Ela guarda só e-mails e domínios, nunca senhas. Se o arquivo estiver corrompido, ninguém além do administrador entra (falha fechada).

## Configurar

```bash
.venv/bin/python start.py configure        # Linux
.\configure-login.ps1                      # Windows
docker compose run --rm app configure      # Docker
```

O assistente pede o issuer, o client ID, o client secret (opcional: Enter cria um cliente público só com PKCE), os e-mails autorizados, a redirect URI e se o 2FA deve ser exigido. Ele grava `QM_OIDC_*` e uma nova `QM_SECRET_KEY` em `local-settings.ps1` e **apaga as antigas linhas `QM_AUTH_USERNAME`/`QM_AUTH_PASSWORD_HASH`**.

No provedor, registre um aplicativo **Web** com:

- Redirect URI: `http://127.0.0.1:8787/auth/callback`. Com outra porta ou acesso pelo Tailscale Serve, use a URL HTTPS da tailnet, por exemplo `https://pc.tailnet.ts.net/auth/callback`, e informe a mesma em `QM_OIDC_REDIRECT_URI`.
- Escopos: `openid email profile`.

| Variável | Obrigatória | Descrição |
|---|---|---|
| `QM_OIDC_ISSUER` | sim | URL do emissor, sempre https (http só em localhost) |
| `QM_OIDC_CLIENT_ID` | sim | ID do aplicativo no provedor |
| `QM_OIDC_CLIENT_SECRET` | não | Segredo do aplicativo (não é senha de usuário) |
| `QM_OIDC_ALLOWED_EMAILS` | sim | E-mails autorizados, separados por vírgula |
| `QM_OIDC_REDIRECT_URI` | não | Padrão `http://127.0.0.1:{QM_WEB_PORT}/auth/callback` |
| `QM_OIDC_REQUIRE_MFA` | não | Padrão `true` |
| `QM_OIDC_MFA_AMR` | não | Valores `amr` aceitos como 2FA |
| `QM_OIDC_ACCEPTED_ACR` | não | Valores `acr` aceitos como 2FA |
| `QM_OIDC_ACR_VALUES` | não | `acr_values` pedido ao provedor (ex. nível com OTP no Keycloak) |
| `QM_OIDC_USER_CLAIM` | não | Claim comparado com a allowlist: `email` (padrão) ou `preferred_username` (padrão para Microsoft) |
| `QM_OIDC_REQUIRE_EMAIL_VERIFIED` | não | Exige `email_verified=true` (padrão `true`; `false` para Microsoft) |
| `QM_OIDC_PROVIDER_LOGOUT` | não | `true` também encerra a sessão no provedor ao sair (cadastre `…/login` como post-logout redirect) |

## Google passo a passo

No **https://console.cloud.google.com**, com a sua conta Google:

1. Crie um projeto, por exemplo `Switch Manager`, no seletor de projetos no topo da página.
2. Vá em **APIs e serviços → Tela de permissão OAuth** (ou **Google Auth Platform**) e clique em **Começar**:
   - Nome do app: `Switch Manager`. E-mail de suporte: o seu.
   - Público-alvo: **Externo**. Use **Interno** se todas as contas forem do mesmo Google Workspace.
   - Com o app em modo **Teste**, só entram os e-mails cadastrados em **Usuários de teste** (até 100). Para escalar, use **Publicar app**. Os escopos usados (`openid email profile`) não exigem revisão de segurança do Google.
3. Em **Clientes → Criar cliente**:
   - Tipo: **Aplicativo da Web**.
   - URIs de redirecionamento autorizados: `http://127.0.0.1:8787/auth/callback`.
4. Copie o **ID do cliente** (termina em `.apps.googleusercontent.com`) e a **Chave secreta do cliente**.
5. Rode `.venv/bin/python start.py configure`:
   - Issuer: aperte Enter, o padrão é `https://accounts.google.com`.
   - Client ID e secret: os do passo 4.
   - E-mails autorizados: os e-mails das contas Google, separados por vírgula.
   - Confirme que todas essas contas têm a **verificação em duas etapas** ativada.

O Google não informa no token se houve 2FA. Por isso o assistente grava `QM_OIDC_REQUIRE_MFA=false` só depois dessa confirmação. A garantia do 2FA passa a ser a configuração de cada conta (**myaccount.google.com → Segurança → Verificação em duas etapas**) ou a política obrigatória do Google Workspace. O e-mail continua precisando estar verificado pelo Google e na allowlist.

## Microsoft (contas corporativas/escola) passo a passo

No portal **https://entra.microsoft.com**, entre com uma conta de administrador da organização:

1. Vá em **Identidade → Aplicativos → Registros de aplicativo → Novo registro**.
   - Nome: `Switch Manager`.
   - Tipos de conta com suporte: **Somente contas neste diretório organizacional (locatário único)**.
   - URI de redirecionamento: plataforma **Web**, `http://127.0.0.1:8787/auth/callback`.
2. Na **Visão geral** do aplicativo, copie o **ID do aplicativo (cliente)** e o **ID do diretório (locatário)**.
3. Em **Certificados e segredos → Novo segredo do cliente**, crie o segredo e copie a coluna **Valor**. Ela só aparece uma vez. Anote a data de expiração para renovar antes.
4. Exija 2FA para essas contas:
   - Com licença Entra ID P1/P2: **Proteção → Acesso Condicional → Nova política**, aplicada aos usuários autorizados e ao aplicativo `Switch Manager`, com **Conceder → Exigir autenticação multifator**.
   - Sem P1: ative a **MFA por usuário** para as contas autorizadas. Os "Padrões de segurança" pedem MFA só em alguns logins, e o Switch Manager recusa os logins em que o token não comprovar MFA.
5. Rode o assistente (`.venv/bin/python start.py configure`):
   - Issuer: `https://login.microsoftonline.com/<ID-do-diretório>/v2.0`, com o GUID do passo 2. Não use `common`, `organizations` nem o domínio da empresa.
   - Client ID e secret: os dos passos 2 e 3.
   - Logins autorizados: o login corporativo (UPN) de cada pessoa, ex. `voce@empresa.com.br`.

Para Microsoft, o app usa automaticamente `QM_OIDC_USER_CLAIM=preferred_username` e `QM_OIDC_REQUIRE_EMAIL_VERIFIED=false`. Os tokens corporativos não trazem `email_verified`, e a Microsoft recomenda não usar o atributo `email` para autorizar. A proteção vem de aceitar apenas tokens assinados por **este** tenant e logins da allowlist.

Se o login for recusado com "não confirmou a autenticação de dois fatores" mesmo com MFA, abra **Configuração de token → Adicionar declaração opcional → ID** e inclua `amr`, se estiver disponível, e confira se a política de MFA está valendo para o aplicativo.

## 2FA por provedor

- **Microsoft Entra ID**: veja a seção anterior. O token traz `amr: ["pwd","mfa"]` quando houve MFA, e o padrão aceita esse valor.
- **Keycloak / Authentik**: configure o fluxo de login com OTP ou WebAuthn obrigatório. Inclua o claim `amr` (mapper "Authentication Method Reference") ou use um nível `acr` e defina `QM_OIDC_ACR_VALUES` e `QM_OIDC_ACCEPTED_ACR` com esse valor.
- **Google**: o ID token do Google **não informa** se houve 2FA. Com o padrão, todo login Google é recusado. Para usar Google, imponha a verificação em duas etapas no Admin do Workspace para todas as contas autorizadas e defina `QM_OIDC_REQUIRE_MFA=false` conscientemente. Nesse caso o aplicativo confia no provedor sem conseguir conferir o 2FA.

## Observações

- O servidor precisa alcançar o provedor (HTTPS de saída) no momento do login. `/api/health` e a página `/login` funcionam sem rede.
- O console serial físico continua sendo o acesso de emergência ao switch se o provedor estiver fora do ar.
