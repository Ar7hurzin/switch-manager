# Segunda rodada de correções — 17/11/2026

Esta rodada trata as oito falhas reproduzidas na segunda revisão e duas diferenças do simulador em relação à documentação de referência. O visual aprovado, a configuração local e os dados do laboratório foram preservados. Não houve acesso ao equipamento físico, commit, push ou PR.

## Correções

1. **Verificação de listas multilinha:** tagged/untagged são lidos como seções completas. VLANs extras em continuação impedem confirmar sucesso; seções repetidas, ausência de listas e conteúdo ambíguo falham de forma explícita.
2. **Identificação inicial:** falha de conexão, timeout ou perfil inválido não publica um snapshot sem identificação. A próxima tentativa repete a identificação antes de consultar portas/VLANs.
3. **Origem das respostas:** o parser de porta exige o cabeçalho correto e rejeita resposta de outra interface ou seções misturadas. A verificação posterior das configurações também exige uma única interface correspondente.
4. **Fila de VLANs:** alternância entre consultas prioritárias e rodízio normal; prioridades giram entre IDs, mesmo quando um inventário truncado reinsere a mesma VLAN constantemente.
5. **Registro de laboratório:** exportação mantém horários e validade por campo, sem renovar taxas antigas quando outro campo da porta é atualizado. Distingue erro global de coleta, idade do conteúdo, cobertura parcial e tabela MAC histórica. Metadados são limitados a uma lista explícita de campos; saídas CLI brutas não são gravadas.
6. **CLI durante logout:** o servidor associa a conexão à sessão antes da abertura, sem depender de o navegador já ter recebido um token. Logout/revogação cancela a abertura em andamento. Ao concluir a abertura, a autorização também é revalidada, cobrindo a revogação ocorrida antes de registrar o proprietário. Uma sessão não encerra a CLI de outra.
7. **Prévias concorrentes:** revisões de solicitação impedem que respostas ou erros antigos substituam a prévia mais recente. Aplicar bloqueia novas prévias, controles e aplicação duplicada até a conclusão.
8. **Envio CLI:** um único envio em andamento cobre botão, Enter e Enviar Enter. Os controles são restaurados ao finalizar, sem reenvio automático. Status atrasado é descartado quando token ou geração da operação já mudou.

## Alinhamento do simulador e prévia

A preparação de trunk/hybrid agora faz uma consulta de leitura do modo atual, valida o perfil e registra esse modo na prévia. Uma passagem por access, quando necessária para trunk ↔ hybrid, aparece explicitamente nos comandos. Quando a porta já está no modo desejado, esse reset intermediário não é adicionado.

Antes de aplicar, o modo é relido. Divergência em relação à prévia exige gerar outra, sem enviar configuração. Por isso, a preparação de trunk/hybrid precisa de console disponível e não pode ocorrer junto com a CLI interativa.

O simulador rejeita a transição direta e mantém o PVID de trunk/hybrid ao excluir a VLAN correspondente. Isso evita sugerir recuperação automática de conectividade após a exclusão. Link físico/simulado UP não comprova funcionamento de tráfego na VLAN.

Referência: [Huawei Quidway S3900 Series Command Manual, Release 1510, seções 1.1.7 e 1.1.27](https://manualzz.com/doc/60019104/huawei-quidway-s3900-series-command-manual). O firmware físico observado é VRP 3.10(CN) RELEASE 0002; o alinhamento documental e o simulador não substituem ensaio nesse firmware. As flags de escrita/CLI não foram habilitadas nem alteradas.

## Validação

A suíte foi ampliada com cenários de recuperação, resposta de interface errada, listas quebradas, starvation de VLANs, taxas antigas exportadas, ações concorrentes, logout durante abertura da CLI e mudança de modo entre prévia e aplicação.

```bash
cd ~/Projetos/switch-manager
.venv/bin/python -m unittest discover -s tests -v
node --test tests/frontend_reliability.cjs
node --check static/app.js
.venv/bin/python -m pip check
```

Para carregar as correções, reinicie a plataforma pelo procedimento habitual e atualize o navegador com Ctrl+Shift+R. A execução continua usando o modo já configurado localmente; a instância física não foi reiniciada automaticamente. Reiniciar o servidor exige entrar novamente, sem redefinir a senha.

Resultado local: **95 testes Python e 15 testes comportamentais da interface aprovados**. Sintaxe JavaScript, dependências e integridade das alterações também aprovadas. A matriz Windows não foi executada nesta máquina.

Instalação isolada: imagem reconstruída com Podman rootless e Python 3.12; **os mesmos 95 testes Python aprovados**, sem rede externa nem dispositivos físicos associados.
