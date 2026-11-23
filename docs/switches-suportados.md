# Switches suportados e detecção automática

Ao conectar o cabo console, o Switch Manager lê o `display version`, identifica o switch e adapta o painel sozinho: nome e modelo, portas (descobertas no próprio switch), VLANs, tabela MAC e tráfego. Não é preciso reiniciar ao trocar de switch.

## Perfis

| Perfil | Switches | Monitoramento | Configuração guiada |
|---|---|---|---|
| `quidway-s3900-si` | Quidway S3900-SI 28-Port, VRP 3.10(CN) RELEASE 0002 | Sim | **Sim (validado no equipamento real)** |
| `vrp3-legacy` | Huawei/Quidway antigos (S3026C, S2403, S3526, S5600…), Huawei-3Com e 3Com com VRP 3.x / Comware 3.x | Sim | **Sim, adaptada** pela sintaxe do VRP 3 (modelo ainda não validado) |
| não suportado | VRP 5/8 (S5700, CloudEngine…), outros fabricantes | Não | Não: **só CLI** |

- Um S3900 com **outro release** de firmware cai no perfil genérico (configuração adaptada).
- A **CLI** funciona com qualquer switch, mas não tem prévia nem proteção: use com cuidado.

## Configuração adaptada e passagem para a CLI

Em modelos ainda não validados, a configuração guiada funciona com as mesmas proteções do S3900:
- prévia com os comandos exatos;
- confirmação de que o switch no cabo é o mesmo da prévia;
- parada no primeiro comando recusado;
- leitura posterior que confirma o resultado.

A prévia mostra o aviso "modelo ainda não validado".

**Se o switch recusar um comando**, a operação inteira (por exemplo, "porta hybrid") passa a ser **só pela CLI naquele modelo e firmware**. Ela aparece como "(use a CLI)" e fica desabilitada, enquanto as outras operações continuam funcionando. Essa marcação fica em `instance/switch-capabilities.json`, para não repetir a tentativa depois de reiniciar. O administrador pode liberar uma nova tentativa (`POST /api/config/capabilities/reset`).

**Atenção:** os comandos anteriores ao recusado já terão sido aplicados, e não há desfazer automático. Exemplo: num trunk, as VLANs antigas são removidas antes de a nova lista ser aplicada. Teste cada operação primeiro num switch de laboratório.

Antes de cada operação, se o console estiver numa sub-visão (por exemplo `[Switch-vlan40]`, deixada por uma CLI fechada), o Manager envia `return` e só continua na visão de usuário. Assim uma recusa de `system-view` por estar na visão errada não marca a operação como "use a CLI". Fechar a CLI também envia `return`.

## Leitura automática

A cada rodada (cerca de 4 segundos mais o tempo do console), o monitoramento:
- lê o **estado de link de todas as portas** numa consulta curta (`display brief interface` ou `display interface brief`, a que o switch aceitar). Se o switch não tiver nenhuma das duas, volta à leitura de uma porta por rodada;
- lê **uma porta em detalhe** (modo, PVID, VLANs e tráfego) em rodízio; uma porta cujo link mudou passa na frente;
- faz uma tarefa extra em rodízio: inventário de VLANs, uma VLAN em detalhe ou a tabela MAC (no máximo uma vez por minuto).

Mensagens de mudança de link que o switch escreve sozinho no console (`PORT LINK STATUS CHANGE`, `Line protocol on the interface ... is UP/DOWN`) também colocam a porta citada na frente da fila.

A 9600 baud o console transmite cerca de 960 bytes por segundo, e isso limita a velocidade. Subir o console do switch para 115200 baud (`user-interface aux 0` → `speed 115200`, e o mesmo valor em `QM_SERIAL_BAUD`) deixa tudo bem mais rápido. Só faça isso com acesso físico ao switch: se os dois lados ficarem diferentes, o console para de responder até alguém acertar.

## Troca de switch

O painel percebe a troca de três formas:
- **Prompt diferente** (`<NomeDoSwitch>`): a identificação é refeita na hora.
- **Leitura que não bate com o switch esperado** (console sem resposta, porta ou resumo de portas não reconhecidos): o `display version` roda na mesma leitura ou na seguinte, sem esperar o intervalo de erro. Assim a troca aparece em poucos segundos mesmo quando os dois switches têm o mesmo nome de fábrica (`<Quidway>`).
- **Releitura do `display version` a cada 60 segundos**, como garantia.

Quando um switch diferente é identificado:
- os dados do switch anterior são descartados;
- **prévias de configuração pendentes são canceladas**: uma prévia nunca é aplicada a outro switch, e o executor também confere a identidade antes de enviar qualquer comando;
- credenciais de console digitadas para o switch anterior são apagadas da memória.

## Login no console

Se o switch pedir usuário e senha no console, o painel mostra **Login no console do switch**:
- As credenciais ficam **só na memória** do programa e são usadas em **um** login logo depois de digitadas.
- Se o console pedir login de novo (tempo de inatividade do switch, porta reaberta ou outro aparelho no cabo), o painel **não** reenvia a senha sozinho: ele mostra o botão **"Mesmo switch: entrar de novo"**. Antes do login nada na linha serial prova qual aparelho está do outro lado, então essa confirmação evita entregar a senha a um dispositivo trocado.
- Nunca são gravadas em disco, logs nem respostas da API.
- São descartadas em quatro situações: ao trocar de switch, quando o switch recusa o login (sem nova tentativa automática, para não bloquear a conta), ao clicar em **Esquecer credenciais** e ao encerrar o programa.

## Validar a configuração guiada no switch real

Para tirar o aviso "modelo ainda não validado" de um modelo (por exemplo, o S3026C), rode o roteiro de validação **com o Switch Manager parado**:

```bash
.venv/bin/python start.py validate
```

No Windows: `.\.venv\Scripts\python.exe start.py validate`.

O roteiro pergunta qual porta livre usar e duas VLANs de teste (padrão 3998 e 3999). Também pergunta se deve testar rotas estáticas (com um próximo salto informado por você) e o `save`. Depois ele:
1. confere, só com comandos `display`, que a porta está na configuração de fábrica e sem link (com link, pede confirmação) e que as VLANs de teste não existem;
2. copia a configuração em execução para `instance/validation/backup-*.txt` (permissão 600);
3. aplica cada operação guiada **pelo mesmo executor do painel**, com a mesma verificação por leitura posterior: criar, renomear e excluir VLAN; porta access, trunk e hybrid; descrição; shutdown/undo shutdown; nome do switch (e de volta ao original); rotas, se escolhidas;
4. devolve a porta à configuração de fábrica, exclui as VLANs de teste, restaura o nome e **confirma lendo a configuração**;
5. só então, se escolhido, testa o `save`.

Nada é salvo na inicialização sem o `save`. Se algo sair errado, reiniciar o switch sem salvar volta tudo ao que era.

O resultado fica em `instance/validated-models.json`, por modelo e firmware. Ao reiniciar, o painel usa esse arquivo:
- operações que passaram perdem o aviso na prévia;
- o aviso do topo mostra quantas operações estão validadas e quais ainda não;
- uma operação que o switch recusou passa a ser feita pela CLI, como no uso normal.

O IP de gestão nunca é testado pelo roteiro, porque mudá-lo pode cortar o acesso ao switch. Por isso ele continua com aviso. Se a limpeza não for confirmada, nada é marcado como validado.

O relatório `instance/validation/validacao-*.txt` traz cada etapa e a saída do console, sem senhas. Envie-o quando uma etapa falhar.

## Validar um modelo novo (captura)

1. Pare o Switch Manager (`Ctrl+C`). A porta serial é exclusiva.
2. Conecte o switch e rode:
   ```bash
   .venv/bin/python start.py capture
   ```
3. A captura envia **apenas** comandos `display` de uma lista fixa: versão, configuração atual, interfaces, VLANs e tabela MAC. Ela remove senhas, communities SNMP e chaves e grava `instance/captures/<modelo>-<data>.txt` (permissão 600).
4. Abra o arquivo, confira que não sobrou dado sensível e envie para análise.
5. Com as saídas reais, os parsers e a sintaxe do perfil são ajustados, e o modelo pode ganhar um perfil próprio **validado**.

As respostas dos modelos S3026C e 3Com 4500 usadas nos testes automáticos (`tests/fake_console.py`) são **sintéticas**, baseadas nos manuais, e serão substituídas pelas capturas reais.
