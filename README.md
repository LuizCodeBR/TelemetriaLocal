# Telemetria Local

Como saber se uma sala continua enviando medições atuais quando o sensor ainda responde? A Telemetria Local simula sensores de temperatura e umidade, guarda leituras durante quedas de comunicação e registra qual medição confirmou a recuperação de cada sala.

**Versão 0.9.1** · Python · MQTT 5 · SQLite · painel no navegador

## Experimente primeiro no navegador

1. Baixe o [ZIP da versão 0.9.1](https://github.com/LuizCodeBR/TelemetriaLocal/releases/download/v0.9.1/TelemetriaLocal-0.9.1.zip) e extraia a pasta inteira.
2. Na pasta extraída, abra **[exemplo-atualidade.html](exemplo-atualidade.html)** no navegador. O exemplo funciona offline, com dados fictícios. Basta um navegador com JavaScript.
3. Clique em **Avançar** três vezes. Observe a sala 01 continuar em comunicação, receber uma leitura atrasada e repetida e, na última etapa, voltar a ter uma medição atual. No detalhe da ocorrência, confira o identificador da leitura e os horários de medição e recebimento.

Para investigar outro período ou exportar um CSV, abra o [histórico interativo dos dois sensores](exemplo-historico.html). A distribuição também contém exemplos de [comunicação](exemplo-comunicacao.html), [alertas de temperatura](exemplo-alertas.html) e [reenvio de medições](exemplo.html).

## Rode a central ou os testes

Use Python **3.12 ou 3.13** com `pip` e `venv`. Execute os comandos no terminal da pasta extraída. Na primeira execução, o iniciador baixa as dependências e prepara o ambiente dentro dessa pasta. O projeto inclui o serviço MQTT local, usa o SQLite do Python e simula os sensores.

| O que fazer | Windows (PowerShell) | Linux ou macOS |
| --- | --- | --- |
| Abrir a central ao vivo | `.\iniciar.cmd iniciar` | `sh iniciar.sh iniciar` |
| Reproduzir o ensaio das duas salas | `.\iniciar.cmd demo-atualidade` | `sh iniciar.sh demo-atualidade` |
| Executar os testes | `.\iniciar.cmd testar` | `sh iniciar.sh testar` |

Com a central em execução, acesse [o painel local](http://127.0.0.1:8765/) e escolha **Atualidade das medições**. Para encerrar, pressione `Ctrl+C`. Os testes de interface usam Edge, Chrome ou Chromium quando disponível.

## Medições desatualizadas por sala

O exemplo offline percorre quatro etapas calculadas com o mesmo banco e as mesmas regras da central. Horários em UTC.

| Horário UTC | Sala 01 no ensaio | Resultado |
| --- | --- | --- |
| 14h20min00s | Última presença e medição de 24 °C confirmadas. | Medição atualizada. |
| 14h20min15s | Continua respondendo. A última medição tem 15 s e o limite é 10 s. | Abre uma ocorrência. A sala 02 continua medindo. |
| 14h20min20s | Chega a leitura das 14h19min50s e o sensor repete o mesmo envio. | A central guarda a leitura uma vez. A ocorrência continua aberta e termina a janela de manutenção. |
| 14h20min25s | Chega uma temperatura de 24 °C medida às 14h20min24s. | O histórico registra a recuperação e o ID da leitura. Foram 5 s em manutenção e 5 s fora dela desde a detecção. |

### Usar o monitor ao vivo

No Windows, execute `.\iniciar.cmd iniciar` e abra **Atualidade das medições** no [painel local](http://127.0.0.1:8765/). O painel principal apresenta presença e atualidade lado a lado. Na página do monitor, selecione uma sala para consultar todo o seu histórico. A visão geral inclui todas as ocorrências abertas e as 50 encerradas mais recentemente. Uma ocorrência longa que termina agora entra na seleção. A tabela apresenta essa seleção pela hora da detecção. No **Histórico por sensor**, o gráfico e a tabela de eventos permitem comparar a desatualização com a manutenção e a comunicação no mesmo período.

O monitor adota o limite de lacuna já configurado para a sala, com padrão de **10 segundos**. Para consultar a configuração, use `.\iniciar.cmd regra --sensor sala-01`. O parâmetro `--lacuna` dessa regra também é usado pelos alertas de temperatura. Cada ocorrência de desatualização guarda o limite que valia quando foi detectada.

- A observação da sala começa com sua primeira presença confirmada ou uma leitura inédita recebida enquanto a central está observando. Ela tem um prazo inicial igual ao limite. Na primeira verificação **acima** do limite, a central registra a detecção. Uma sala que responde e nunca enviou uma leitura também pode gerar uma ocorrência.
- Uma leitura mais recente pelo horário de medição, com idade dentro do limite da ocorrência, confirma a recuperação. O banco guarda seu ID, hora de medição e hora de recebimento. A temperatura pode ser idêntica à anterior. Um horário de medição posterior ao próprio recebimento fica disponível no histórico bruto e é desconsiderado para avaliar a atualidade.
- A central grava a recuperação e a leitura na mesma transação antes de confirmar o envio ao sensor. Leituras antigas continuam no histórico e um reenvio idêntico usa o recibo existente. As respostas de presença continuam sendo medidas separadamente.
- Quando a central deixa de observar ou reinicia, a ocorrência termina no último ponto comprovado com a indicação **Observação interrompida**. A central retoma a avaliação quando volta a observar a sala. O histórico conserva as janelas de manutenção coincidentes e divide a duração observada dentro e fora delas. Uma janela cancelada fica fora do cálculo.

Para gerar outro ensaio em uma pasta isolada, use `.\iniciar.cmd demo-atualidade`. Para executar os testes, use `.\iniciar.cmd testar`. No Linux ou macOS, use `sh iniciar.sh` no lugar de `.\iniciar.cmd`. Os comandos usam Python 3.12 ou 3.13 e as dependências existentes do projeto. O iniciador prepara o ambiente na pasta extraída, com acesso à internet na primeira instalação das dependências. Para o HTML pronto, basta um navegador com JavaScript.

**Instalações existentes.** Encerre a central anterior, extraia a 0.9.1 e informe a pasta de dados habitual com `--dados`. A primeira abertura atualiza o SQLite para a versão 9 em uma transação. Medições, recibos, ocorrências de desatualização, incidentes e agendamentos existentes são preservados. Os resultados já encerrados na versão anterior permanecem como foram registrados.

### O que acontece com atrasos e reinícios

- Se duas leituras válidas chegam juntas, a primeira gravação que satisfaz o limite da ocorrência define a recuperação. O detalhe guarda o identificador e os horários dessa leitura. A medição atual da sala pode avançar com a segunda. Cada reenvio idêntico recebe o mesmo resultado de duplicidade.
- Se uma leitura inédita chega após 15 segundos ou mais sem verificação de presença da central, o SQLite grava o recebimento e encerra as ocorrências abertas de todas as salas no último instante comprovado de cada uma. O histórico mostra **Observação interrompida**. Uma correção para trás no relógio não altera esse encerramento. Uma consulta feita enquanto a central está parada apenas apresenta o limite observado. A confirmação em disco ocorre quando a central volta a gravar uma leitura que comprova a lacuna ou quando o observador encerra a janela.
- Ao retomar a observação de uma sala, a central usa o instante já confirmado para buscar a última medição válida. Isso preserva a referência mesmo quando o relógio volta alguns segundos. Se o processo cair durante uma recuperação, o SQLite descarta a transação incompleta. Se cair depois de confirmar a gravação e antes de responder ao sensor, a repetição encontra o recibo já salvo.
- No painel ao vivo, o temporizador aguarda a consulta em andamento. Ao mudar de sala, o painel descarta respostas da seleção anterior. Se uma sala deixar de existir na pasta de dados em uso, o painel busca novamente a visão de todas as salas.

## Programar uma manutenção por sala

Extraia o ZIP e abra **[a demonstração de manutenção](exemplo-manutencao.html)**. O HTML inclui o formulário, a linha do tempo e o relatório com CSV. Basta um navegador com JavaScript. Use **Avançar até 14h27 e abrir relatório** para comparar duas salas que perderam comunicação das 14h05 às 14h27 UTC.

| Sala | Janela programada | Interrupção observada | Dentro da janela | Fora da janela |
| --- | --- | ---: | ---: | ---: |
| sala-01 | Troca de bateria, 14h00–14h20 | 22 min | 15 min | 7 min |
| sala-02 | Nenhuma | 22 min | 0 min | 22 min |

O relatório e o CSV expressam as durações em segundos. Para a sala-01, procure **900 s** dentro e **420 s** fora. Em **Reiniciar demonstração**, volte às 13h55, cancele a troca da sala-01 e agende a sala-02 no mesmo intervalo. Avance outra vez e compare o resultado. As alterações do HTML valem enquanto a demonstração estiver aberta.

### Usar na central local

1. Extraia a pasta do projeto no disco desejado. No Windows, execute `.\iniciar.cmd iniciar` e abra [o painel local](http://127.0.0.1:8765/). O simulador registra a primeira sala. No Linux e macOS, use `sh iniciar.sh iniciar`.
2. Abra **Manutenção**, escolha uma sala já conhecida, informe o início e o fim em **UTC** e um motivo de até 160 caracteres. Clique em **Agendar**. A página consulta a central a cada 15 segundos e oferece o botão **Atualizar**. Uma janela futura pode ser cancelada. A agenda conserva o motivo, o período e o horário do cancelamento.
3. Durante a janela, o monitor ao vivo exibe o motivo e o horário de término na linha da sala. Em **Incidentes**, selecione uma queda para ver as janelas coincidentes e a duração dentro e fora delas. Em **Histórico por sensor**, a faixa roxa no gráfico e a tabela de eventos situam a manutenção no período escolhido.
4. Selecione **Exportar resumo CSV** no relatório de disponibilidade. As colunas `interrupcao_em_manutencao_s` e `interrupcao_fora_de_manutencao_s` somam o total observado de `em_interrupcao_s`. O cálculo do percentual continua sendo tempo em comunicação dividido pelo tempo avaliado. Períodos sem observação da central continuam identificados.

### Regras e persistência

As janelas são avulsas e o fim é exclusivo. Uma segunda janela pode começar no instante em que a primeira termina. Salas diferentes aceitam o mesmo horário. O SQLite serializa o cadastro e o cancelamento por transação. Ao conseguir a escrita, a central confere novamente a hora, a sala e os conflitos. Uma repetição do mesmo envio recebe o comprovante da primeira gravação. A partir do início, a janela integra o histórico da sala. Uma janela cancelada antes do início libera o horário e sai dos cálculos.

O monitor continua coletando evidência durante a manutenção. Apenas a interseção entre uma interrupção observada e uma janela válida conta como interrupção em manutenção. A coincidência de horários, por si só, não identifica a causa da falha.

A agenda de manutenção foi introduzida com a versão 7 do banco. A versão 0.9.1 preserva esses registros ao atualizar a pasta de dados para a versão 9. Recarregue as abas depois de atualizar. Para reproduzir o ensaio em uma base separada, execute `.\iniciar.cmd demo-manutencao`. Para verificar a aplicação, use `.\iniciar.cmd testar`. Em Linux e macOS, substitua `.\iniciar.cmd` por `sh iniciar.sh`. Para esses comandos, use Python 3.12 ou 3.13. O iniciador prepara as dependências na pasta extraída na primeira execução. O HTML pronto funciona offline desde a primeira abertura.

### Revisão de persistência e concorrência na versão 0.8.1

Se a resposta se perder depois de clicar em **Agendar**, a aba ao vivo conserva a identificação do envio. Ao recarregar a mesma aba e consultar a mesma base, o formulário recupera a última tentativa sem confirmação. Repetir o envio consulta o resultado original. Se outra pessoa já cancelou a janela, a agenda mostra o horário do cancelamento. Depois dessa confirmação, um novo clique pode agendar outra janela. Com o armazenamento de sessão bloqueado no navegador, a recuperação funciona somente enquanto a página permanece aberta.

A central consulta os recibos já gravados mesmo enquanto outra conexão está escrevendo. Para uma mudança nova, ela reserva a escrita e confere novamente a janela e o relógio. Se o relógio do computador recuar, o último horário já gravado de uma nota ou reconhecimento de incidente também limita a hora da agenda. Isso evita cancelar retroativamente uma manutenção cujo início já passou segundo o histórico da central.

Na demonstração offline, depois de **Avançar até 14h27 e abrir relatório**, você pode filtrar uma sala ou um período e agendar uma intervenção futura. A linha do tempo e o CSV continuam usando o recorte selecionado. As mudanças feitas na simulação usam a memória da página.

Instalações 0.8.0 conservam a versão 7 do banco. Na primeira abertura com a 0.8.1, a central acrescenta um índice de horários para consultar o histórico das ações do operador. A revisão inclui testes com encerramento abrupto antes e depois da gravação, cancelamento por outra aba, janelas contíguas na virada da meia-noite UTC e comparação entre a linha do tempo, o relatório e o CSV.

## Acompanhar um incidente de comunicação

Abra **[a demonstração de incidentes](exemplo-incidentes.html)** depois de extrair o ZIP. Ela funciona com JavaScript no navegador, inclusive se você mover apenas esse HTML para outra pasta. Selecione a **sala-01**, clique em **Marcar como visto**, escreva uma **Nota do operador** e escolha **Simular retomada**. Abra o link das entregas para investigar as medições recebidas na reconexão.

| Horário UTC no ensaio | O que aparece |
| --- | --- |
| 14:00:15 | A central detectou a interrupção da sala-01. A sala-02 continua respondendo. |
| 14:00:20 | Momento simulado das ações que você fizer antes de avançar. Marcar como visto nesse ponto produz um tempo de reconhecimento de 5 segundos. |
| 14:00:32 | O retorno de sinal encerra a interrupção após 17 segundos. As seis medições da fila da sala-01 são consultáveis no relatório de entregas. |

O HTML usa em memória os dados de uma sessão MQTT com dois sensores. **Reiniciar demonstração** ou reabrir o arquivo começa outro ensaio. Na central local, os reconhecimentos e notas são persistentes no SQLite.

### Como usar na central local

1. Abra dois terminais na pasta do projeto. No primeiro, execute `.\iniciar.cmd central`. No segundo, execute `.\iniciar.cmd sensor --sensor sala-01`.
2. Abra o painel indicado pelo primeiro terminal, selecione **Incidentes** e aguarde o primeiro contato do sensor. Para provocar uma queda, encerre apenas o sensor com **Ctrl+C** e mantenha a central ligada. Após o prazo de detecção de 15 segundos, a queda aparece na lista. A página consulta a central a cada cinco segundos enquanto está visível e também oferece **Atualizar**.
3. Filtre por sala, **Em aberto** ou **Ainda não vistas**. Selecione a ocorrência, marque-a como vista e acrescente notas. O detalhe continua aberto se uma ação tirar a ocorrência do filtro. O painel usa o horário da central para registrar as ações.
4. Reinicie o sensor com o mesmo comando. A linha do tempo registra a retomada. O atalho abre o relatório de entregas na sala e no intervalo da detecção até um minuto após a retomada. A duração observada e o tempo até o primeiro reconhecimento ficam no detalhe.

Se a central perder a capacidade de observar, a linha do tempo exibe **Observação interrompida** no último ponto confirmado. As notas também podem ser acrescentadas depois da retomada. Na central local, o registro identifica a ação pelo texto e pelo horário. As abas compartilham o mesmo acompanhamento.

No Windows, `.\iniciar.cmd demo-incidentes` gera outro HTML em uma sessão isolada. Para executar os testes, use `.\iniciar.cmd testar`. No Linux/macOS, use `sh iniciar.sh demo-incidentes` ou `sh iniciar.sh testar`. A primeira execução do simulador em uma máquina nova requer Python 3.12 ou 3.13 e acesso às dependências indicadas em `requirements.txt`. A demonstração pronta abre diretamente no navegador.

### Persistência e uso por duas abas

A central vincula cada ação ao identificador da queda. O SQLite aceita um reconhecimento por ocorrência. O navegador atribui um identificador de envio a cada nota. Se o navegador precisar repetir o envio, a central devolve a confirmação existente. Duas notas enviadas separadamente permanecem no histórico, inclusive se tiverem o mesmo texto. O monitor continua a registrar o retorno do sinal na mesma ocorrência.

### Revisão de persistência e concorrência na versão 0.7.1

A central atribui uma identidade permanente a cada queda. Se uma aba ficar aberta enquanto você troca a pasta usada pela central, ela confere essa identidade antes de gravar. Duas bases criadas separadamente podem ter a ocorrência nº 1. Uma ação preparada para a primeira continua vinculada àquela queda. Depois de trocar a versão da central, recarregue as abas abertas.

Se a resposta de um envio se perder, a aba ao vivo guarda o texto enviado e a chave da tentativa no armazenamento de sessão do navegador. Ao recarregar a mesma aba e abrir a mesma ocorrência, você pode repetir o envio. A central confirma uma única nota. Depois da confirmação, escrever o mesmo texto de novo cria outra anotação. Se o navegador bloquear o armazenamento de sessão, a tentativa fica disponível enquanto a página estiver aberta. A demonstração offline continua usando memória.

O primeiro reconhecimento conserva seu horário. Repetir um reconhecimento ou uma nota já gravada consulta a confirmação existente, inclusive quando outra conexão está escrevendo. Ao registrar uma ação, a central respeita o último instante de observação confirmado da queda se o relógio recuar.

A retomada e a posição dela entre as ações do operador são gravadas juntas. A linha do tempo preserva essa sequência quando dois horários empatam e avisa se o relógio recuou. **Observação interrompida** continua indicando o último ponto confirmado da central. Para quedas já recuperadas em versões antigas, o painel usa os horários disponíveis e identifica a limitação de ordem quando há ações do operador.

A versão 0.7.1 introduziu a versão 6 do banco para preservar a identidade das quedas, os reconhecimentos e as notas. A versão 0.8.0 também incorpora essa migração ao atualizar instalações mais antigas.

## Quanto tempo uma medição levou para chegar?

Abra **[Atraso de entrega por sensor](exemplo-entregas.html)** após extrair o ZIP. O navegador abre uma demonstração completa de duas salas. Mude o limite de **10** para **20** segundos, selecione a sala-01 e experimente os dois botões de exportação. O HTML funciona offline, inclusive em uma pasta diferente do projeto.

| Sala | Entregas | Acima de 10 s | Acima de 20 s | Atraso mediano | Maior atraso |
| --- | ---: | ---: | ---: | ---: | ---: |
| sala-01 | 7 | 4 | 2 | 12 s | 27 s |
| sala-02 | 6 | 0 | 0 | 0 s | 0 s |

Na sala-01, seis medições guardadas na fila chegaram juntas às 14:00:32 UTC. A primeira havia sido medida às 14:00:05 e a última às 14:00:30. Para isolar esse lote, aplique o período de recebimento de **15/01/2026 14:00:32 a 14:00:33 UTC**. Os horários são controlados na simulação para que o resultado possa ser reproduzido.

### Como interpretar e exportar

- O período inclui o início e termina imediatamente antes do fim escolhido. Uma medição anterior ao período entra no relatório quando a central a recebe dentro do filtro. Cada identidade de medição conta uma vez por sensor e conserva o primeiro recebimento gravado.
- O atraso inclui todo o tempo entre a medição e o primeiro recebimento, inclusive a espera na fila. O limite aceita zero e até seis casas decimais, com vírgula ou ponto. Apenas valores **acima** do limite contam como atrasados.
- Um horário de medição posterior ao recebimento aparece como **Conferir relógio**. Ele entra no total recebido e fica fora das contagens de prazo, da mediana e do máximo. Uma sala sem entregas mostra **Sem medições recebidas**. Uma sala com entregas válidas imediatas tem mediana e máximo de **0 s**. Para um número par de atrasos válidos, a mediana é a média dos dois valores centrais.
- **Exportar comparação CSV** inclui todas as salas. **Exportar entregas CSV** inclui todas as páginas da sala selecionada. Ambos registram o período UTC e o limite aplicado. O arquivo detalhado também contém ID, medição, recebimento, atraso em segundos e situação. Valores indisponíveis ficam vazios. Os arquivos usam UTF-8 com marca de codificação, ponto e vírgula como separador e vírgula decimal.

### O que mudou na revisão 0.6.1

Uma sala com regra de temperatura salva já aparece na comparação enquanto aguarda a primeira entrega. Os períodos **Última hora** e **Últimas 24 horas** usam o relógio da central no início da leitura do banco, antes de percorrer o catálogo. As salas e as medições do relatório correspondem à mesma consulta.

A validação lê o valor completo digitado ou colado no limite. Se ele exceder as 15 posições inteiras ou seis casas decimais, a página pede correção e desabilita as duas exportações. O nome do CSV inclui o limite aplicado. No arquivo detalhado, um til antes de cada letra maiúscula do nome da sala permite distinguir, por exemplo, `Sala` e `sala` também no Windows. A coluna de sensor conserva a identificação usada na tabela, com o escape de planilha já previsto para nomes que começam com hífen.

Os testes conferem as tabelas e os dois CSVs com uma contagem em Python para quatro salas, cinco períodos e cinco limites. Outro teste verifica todos os bytes de um CSV com 1.031 entregas, inclusive após trocar de sala e alterar o limite. O ensaio de escrita simultânea cobre o reenvio do mesmo ID enquanto a primeira gravação aguarda confirmação no banco e o uso desse ID por outra sala.

### Como reproduzir

No Windows, execute `.\iniciar.cmd demo-entregas` na pasta extraída para gerar outra demonstração com MQTT local. No Linux/macOS, use `sh iniciar.sh demo-entregas`. O terminal informa o caminho do novo HTML. Para testar com o simulador em funcionamento, execute `.\iniciar.cmd iniciar` e abra **Atraso de entrega** no painel da central. Para conferir a suíte, use `.\iniciar.cmd testar` ou `sh iniciar.sh testar`.

O relatório usa o Python e o SQLite já incluídos na arquitetura do projeto. Na primeira abertura da central, esta versão acrescenta um índice por horário de recebimento às bases existentes. Cada consulta lê as salas e as entregas em uma única transação. Ajustar o limite, selecionar outra sala ou exportar reutiliza os dados da consulta exibida. Aplicar o período novamente consulta os novos recebimentos. O volume carregado na memória do navegador acompanha a quantidade de entregas no intervalo escolhido.

## Comparar a disponibilidade das salas

Abra **[o relatório interativo de disponibilidade](exemplo-disponibilidade.html)** depois de extrair o ZIP. Ele inicia no intervalo de 15/01/2026, das 14:00:00 até 14:00:42 UTC. Para comparar, filtrar e exportar o resumo basta um navegador com JavaScript.

| Sala | Em comunicação | Em interrupção | Avaliado | Central sem observação | Disponibilidade |
| --- | ---: | ---: | ---: | ---: | ---: |
| sala-01 | 15 s | 17 s | 32 s | 10 s | 46,9% |
| sala-02 | 32 s | 0 s | 32 s | 10 s | 100% |

A central detectou a perda da primeira sala às 14:00:15. A resposta às 14:00:32 encerrou a interrupção. Os primeiros 15 segundos incluem a tolerância do monitor. A própria central ficou sem observar de 14:00:32 a 14:00:42. Por isso, a primeira sala tem 15 ÷ 32 de disponibilidade e a segunda tem 32 ÷ 32. Clique no nome de uma sala para investigar o gráfico do mesmo intervalo. **Exportar resumo CSV** grava uma linha por sala com os limites UTC, o percentual e as durações em segundos. **Exportar CSV**, junto ao gráfico, grava as medições individuais do sensor.

O tempo avaliado reúne a comunicação e as interrupções detectadas. O restante do período se divide entre lacunas registradas da central e tempo com evidência insuficiente, que inclui o trecho anterior à entrada de um sensor e horários fora da amostra. Cada sala começa a ser avaliada na primeira presença confirmada da janela. Para um sensor já conhecido que permanece ausente após um reinício, a avaliação pode começar pela detecção da interrupção. Uma ocorrência aberta vai até o último ponto de observação confirmado. A maior interrupção considera somente a duração dentro do filtro. Com zero tempo avaliado, a tabela indica **Sem dados suficientes** e a célula de percentual do CSV fica vazia. Um período avaliado inteiramente em interrupção resulta em **0%**. As durações preservam microssegundos. A tabela e o CSV arredondam o percentual para uma casa decimal e acrescentam casas quando isso é necessário para distinguir uma interrupção curta de 100% ou um contato curto de 0%.

Para gerar uma nova sessão no Windows, execute `.\iniciar.cmd demo-disponibilidade`. No Linux/macOS, use `sh iniciar.sh demo-disponibilidade`. O terminal imprime o caminho do HTML offline gerado. No modo ao vivo, abra **Histórico por sensor** no painel da central. O relatório e o histórico compartilham o filtro e a mesma transação de leitura. O conjunto de testes pode ser executado com `.\iniciar.cmd testar` ou `sh iniciar.sh testar`.

### Como conferir o relatório

Na versão 0.5.1, um ajuste para trás no relógio do computador preserva os intervalos já gravados. Se a central estiver parada, a lacuna em andamento só se estende até o instante da consulta. A coluna **Evidência insuficiente** inclui o tempo pedido além da última informação disponível.

Se você abrir a página antes de ligar os sensores, clique em **Aplicar** depois do primeiro sinal. A consulta atualiza as salas e o seletor. Se uma gravação terminar durante a leitura, todas as linhas, o gráfico e o CSV da tela usam a mesma versão dos dados. Ao aplicar o filtro novamente ou abrir outra sala, a central pode mostrar dados que acabaram de chegar. O CSV registra os limites UTC exibidos na consulta, com o fim exclusivo.

Os testes comparam duas salas com contatos em momentos diferentes e uma terceira que enviou apenas temperatura. A contagem segundo a segundo confere 13 períodos, incluindo cortes nos instantes de queda, de retorno e de parada da central. O teste lê a tabela e o conteúdo exportado no navegador, tanto com respostas da central quanto com o exemplo offline. Outro ensaio grava uma retomada da central e a entrada de uma sala enquanto o relatório está sendo lido.

**Atualização de bases existentes.** Encerre a central antiga, extraia a nova versão e indique a pasta de dados existente com `--dados`. A versão 0.5.1 usa a mesma estrutura da 0.5.0. Ao abrir uma base mais antiga, a central atualiza a estrutura do SQLite para a versão 4 e passa a registrar a primeira presença e o início da avaliação de cada sala em cada janela. Para registros antigos, aproveita apenas os horários comprovados de contato, detecção ou retomada. Uma leitura de temperatura reenviada nunca cria presença retroativa. Após a atualização, use a central nova para abrir essa base.

## Investigar uma sala pelo histórico

1. Abra o exemplo e mantenha a `sala-01` selecionada. Na faixa das 14:00:15 às 14:00:32 UTC, a central detectou uma interrupção. Selecione o ponto ou a linha da medição das **14:00:20**. O detalhe mostra que ela chegou às **14:00:32**, junto com outras cinco medições acumuladas.
2. Troque para a `sala-02`. Ela manteve contato durante a interrupção da primeira sala. A faixa cinza das **14:00:32 às 14:00:42** aparece nas duas salas porque a própria central ficou sem observar.
3. Na `sala-01`, escolha **Personalizado** e aplique o intervalo de **15/01/2026 14:00:15** a **15/01/2026 14:00:30** UTC. O resultado tem três medições, às 14:00:15, 14:00:20 e 14:00:25. O fim é exclusivo, de modo que a leitura das 14:00:30 entra no recorte seguinte. Uma interrupção que começou antes do recorte continua visível enquanto tocar o período.
4. Use **Exportar CSV**. O arquivo contém todas as medições do filtro, inclusive as de outras páginas da tabela. As colunas incluem sensor, ID, horário medido e recebido em UTC, temperatura e umidade. O arquivo usa UTF-8 com marca de codificação, ponto e vírgula entre colunas e vírgula decimal. Um identificador de sensor iniciado por hífen recebe um apóstrofo no CSV para leitura segura em planilhas. Para guardar o arquivo no D:, selecione a pasta de destino na janela de download do navegador.

### Consistência do histórico na versão 0.4.1

Os atalhos de uma hora e 24 horas usam o relógio UTC da central no modo ao vivo. O navegador recebe os dois limites que a central aplicou. O fim desses atalhos tem precisão de milissegundo para que o mesmo recorte possa ser repetido pelos controles de data. A API aceita limites personalizados com precisão de microssegundo. Zeros adicionais são aceitos. Uma fração mais precisa que mudaria o filtro gera uma mensagem de filtro inválido.

As medições seguem o intervalo `[início, fim)`. Uma interrupção ocupa a parte em que sua duração coincide com o recorte. Uma detecção ou uma retomada exatamente no início também aparece, mesmo que seja apenas um instante. Se o último ponto confirmado de uma interrupção anterior coincide com o início e a central deixou de observar, o painel mostra a lacuna da central. A duração do sensor termina no ponto confirmado.

Nos recortes curtos, o eixo mostra frações de segundo. Quando duas ou mais medições ocupam o mesmo ponto da tela, clique nele e use **Próxima medição deste ponto** para percorrer os IDs, valores e horários. Cada medição mantém sua linha na tabela e no CSV. No HTML offline, as faixas do gráfico terminam nos limites da amostra gravada. O painel informa a cobertura disponível ao escolher um período maior.

Cada consulta usa uma única transação de leitura no SQLite. Na geração da demonstração, as duas salas compartilham essa transação. Se você trocar de sensor ou editar as datas enquanto aguarda uma consulta, o botão de CSV aguarda o resultado do filtro aplicado. Uma resposta antiga que chega depois preserva o resultado mais recente. O CSV é formado em blocos a partir dos dados exibidos e seu nome identifica o sensor e os dois limites UTC, com seis casas decimais. Para distinguir campos numéricos de IDs ao abrir em uma planilha, importe a coluna `id` como texto.

A revisão 0.4.1 manteve a estrutura do banco da 0.4.0. A atualização para a 0.5.0 está descrita na seção de disponibilidade.

### Usar com dados locais

No Windows, execute `.\iniciar.cmd`, abra `http://127.0.0.1:8765` e entre em **Histórico por sensor**. Os atalhos **Última hora** e **Últimas 24 horas** usam o horário atual na central local e o fim do ensaio no exemplo offline. **Aplicar** atualiza o recorte mantendo o sensor. Gráfico, tabela e download usam o mesmo conjunto de registros até a próxima consulta. O gráfico posiciona cada ponto pelo horário de medição. Os eventos de comunicação seguem o relógio da central. A tabela mantém os horários completos e as leituras que compartilham um mesmo instante.

Para gerar outro exemplo, execute `.\iniciar.cmd demo-historico` ou `sh iniciar.sh demo-historico`. O último caminho exibido aponta para um novo `historico.html` em uma sessão isolada. Use `--dados` se quiser indicar outra pasta no D:. Para aproveitar uma base da 0.3.1, encerre a central antiga e passe à nova central a mesma pasta com `--dados`. A primeira abertura na versão 0.5.0 atualiza a estrutura para registrar as evidências de disponibilidade.

Para executar os testes, use `.\iniciar.cmd testar` ou `sh iniciar.sh testar`. A suíte cobre filtros em UTC, horários com frações de segundo, reenvios, eventos que cruzam o período e uma consulta enquanto outra conexão grava. Quando Edge, Chrome ou Chromium está disponível, também testa os filtros e o conteúdo do CSV pelo navegador. Para abrir o exemplo basta um navegador com JavaScript. Para executar a central ou os testes, use Python 3.12 ou 3.13. A configuração inicial das dependências requer internet.

## Experimentar o monitor de comunicação

Na pasta extraída, execute `.\iniciar.cmd demo-comunicacao` no Windows ou `sh iniciar.sh demo-comunicacao` no Linux/macOS. O ensaio usa o serviço MQTT do projeto e um relógio controlado para reproduzir o mesmo cenário em poucos segundos. Cada execução grava os bancos e um novo `relatorio.html` em sua própria subpasta de `dados/demonstracoes`. O relatório retrata o instante final do ensaio.

| Horário da central (UTC) | Sala 01 | Sala 02 |
| --- | --- | --- |
| 14:00:00 | Responde e perde a conexão logo depois. Continua medindo. | Responde normalmente. |
| 14:00:15 | Completa 15 s sem responder. A central abre uma interrupção. | Continua respondendo a cada rodada. |
| 14:00:32 | Entrega seis temperaturas antigas com seus horários originais. A interrupção continua aberta até a resposta de presença da rodada atual. | Continua em comunicação, com zero interrupções. |

A última resposta e a retomada ficaram separadas por **32 s**. A interrupção ficou aberta por **17 s**, entre a detecção e a retomada. A central conhece esses três horários. O instante físico em que o equipamento perdeu a rede pode cair entre duas rodadas.

### Como a central reconhece a presença

A cada cinco segundos a central envia um identificador de rodada por MQTT. Cada sensor devolve esse identificador pelo seu próprio tópico de presença. A central usa seu relógio UTC para datar a primeira resposta válida de cada sensor na rodada. Uma resposta atrasada de outra rodada é descartada. Repetir a resposta atual preserva o horário do primeiro contato. Os sinais de presença são passageiros. A fila local continua armazenando as temperaturas, que podem chegar muito depois de terem sido medidas.

Ao completar 15 segundos de observação sem uma resposta de um sensor conhecido, o monitor abre uma interrupção. A próxima resposta válida registra a retomada. Enquanto a central está ativa, ela grava um ponto de observação normalmente a cada segundo. Se a central parar ou ficar pelo menos 15 segundos sem conseguir observar, o histórico mostra o intervalo como **sem observação** a partir do último ponto confirmado. Uma interrupção que já estava aberta fica encerrada naquele ponto com o resultado **Observação interrompida**. Ao voltar, a central aguarda até 15 segundos por um novo sinal de cada sensor conhecido.

Apenas uma central por pasta de dados pode registrar presença ao mesmo tempo. Ao abrir bases da versão 0.2.1, os sensores com medições e sem resposta de presença aparecem como **Sem sinal registrado** até participarem de uma rodada. O painel exibe as interrupções abertas, as 50 últimas encerradas e os 20 últimos intervalos encerrados sem observação, além de uma eventual lacuna em andamento.

Para consultar o histórico completo de um sensor da demonstração, substitua o caminho abaixo pela pasta que contém o `relatorio.html` indicado no terminal.

~~~powershell
.\iniciar.cmd comunicacao --sensor sala-01 --dados "D:\caminho\da\sessao"
~~~

Para acompanhar o monitor em tempo real, abra três terminais na mesma pasta. Inicie `.\iniciar.cmd central`, `.\iniciar.cmd sensor --sensor sala-01` e `.\iniciar.cmd sensor --sensor sala-02`, um por terminal. Abra o painel em `http://127.0.0.1:8765` e aguarde os dois sensores aparecerem **Em comunicação**. Encerre a sala-01 com Ctrl+C, mantenha os outros dois terminais abertos e aguarde a indicação **Sem comunicação**. Reinicie o mesmo comando da sala-01 para ver a retomada. Use `.\iniciar.cmd comunicacao --sensor sala-01` para consultar essa sessão ao vivo.

### Persistência e concorrência na versão 0.3.1

Quando duas operações precisam gravar ao mesmo tempo, o SQLite permite que uma delas reserve a escrita. A central confere o relógio novamente quando consegue essa reserva. Em uma espera curta, registra a retomada com o horário em que recebeu a resposta MQTT. Ao completar 15 segundos desde o último ponto de observação confirmado, encerra a janela naquele ponto, registra uma lacuna e inicia uma nova rodada. A resposta que estava pendente pertence à janela anterior. Uma central que inicia com o banco ocupado também registra o início da observação somente quando consegue gravar.

O painel ordena as 50 ocorrências encerradas pelo horário de encerramento. Uma interrupção que durou vários dias e terminou agora entra nessa lista. Empates usam o identificador da ocorrência como segundo critério. Se a central parar abruptamente, a consulta ao vivo usa o último ponto confirmado para apresentar o limite da observação. Ao reiniciar, a central grava esse encerramento e a ordenação do painel permanece a mesma. Durante a observação ativa, o painel continua mostrando todas as interrupções em aberto.

Para atualizar a versão 0.3.0, encerre a central antiga, extraia a 0.3.1 em outra pasta e inicie a nova central com `--dados` apontando para a pasta de dados existente. A primeira abertura cria um índice para consultar os encerramentos e atualiza a versão da estrutura do SQLite. As leituras, os contatos, as interrupções e as retomadas já gravados são preservados. Use a versão nova nas próximas aberturas dessa base. O monitor aplica a regra de espera a partir da atualização. Os dados antigos não contêm os tempos de espera necessários para reclassificar ocorrências passadas.

Para reproduzir os testes da revisão junto com os demais, execute `.\iniciar.cmd testar` no Windows ou `sh iniciar.sh testar` no Linux/macOS. Os ensaios usam bancos temporários na pasta do projeto. Incluem um escritor que bloqueia a confirmação de uma resposta MQTT, uma consulta mantida aberta durante uma retomada, duas aberturas simultâneas de uma base anterior e encerramentos abruptos de um processo antes e depois da confirmação da transação.

## Experimentar os alertas

Na pasta extraída, rode a demonstração com Python 3.12 ou 3.13. A primeira execução prepara as dependências no diretório do projeto. Depois, os testes e as cinco demonstrações funcionam offline.

~~~powershell
.\iniciar.cmd demo-alertas
~~~

No Linux/macOS, use `sh iniciar.sh demo-alertas`. O comando cria uma sessão isolada em `dados/demonstracoes`, envia por MQTT um perfil simulado em UTC e imprime o caminho do novo `relatorio.html`. O ensaio percorre cinco minutos de medições sem esperar cinco minutos no relógio.

| Etapa | Resultado esperado |
| --- | --- |
| Primeiro lote após a volta da central | 31 medições. As duas lacunas no perfil dividem o episódio em duas ocorrências provisórias. |
| Chegada das duas medições atrasadas | 33 medições únicas. O histórico recompõe um episódio de 14h02 a 14h07 UTC, com duração de 5 min e pico de 31,4 °C. |
| Reenvio do pico com o mesmo ID | Permanecem as 33 medições e uma ocorrência recuperada. |

## Como interpretar e configurar as ocorrências

A regra padrão é aplicada separadamente a cada sensor. Três instantes consecutivos com temperatura **maior ou igual a 28 °C** abrem uma ocorrência. O início é o primeiro desses instantes. A ocorrência continua aberta mesmo que a temperatura caia a 27 °C e termina na primeira medição **menor ou igual a 26 °C**. O pico inclui as três medições iniciais.

Um intervalo **superior a 10 segundos** entre medições consecutivas é registrado como lacuna. Uma sequência ainda não confirmada recomeça. Se o episódio já estava aberto, fica marcado como **interrompido por lacuna** na última observação antes da interrupção. Esse horário delimita a cobertura conhecida. O painel ao vivo sinaliza sensores sem medição recente e a duração de uma ocorrência aberta vai até a última medição observada.

O painel mostra todas as ocorrências abertas, as 50 últimas finalizadas e as 20 últimas lacunas. A consulta por sensor inclui todo o histórico. Os comandos abaixo usam a pasta da central. Se escolheu uma pasta própria ao iniciar a central, use o mesmo `--dados`.

~~~powershell
.\iniciar.cmd regra --sensor sala-01
.\iniciar.cmd regra --sensor sala-01 --alta 30 --recuperacao 27 --lacuna 15
.\iniciar.cmd ocorrencias --sensor sala-01
~~~

O primeiro comando consulta a regra. O segundo salva os limites e **recalcula todo o histórico desse sensor** com a regra escolhida. Os demais sensores conservam suas regras. Para restaurar os padrões, use `--alta 28 --recuperacao 26 --lacuna 10`. O simulador comum varia perto de 24 °C, por isso `demo-alertas` é o caminho reproduzível para observar um aquecimento.

### Leituras atrasadas e persistência

A central avalia o horário UTC da medição. Leituras que chegam em sequência atualizam o estado do sensor de forma incremental. Ao chegar uma medição atrasada que muda a série, a central reavalia o trecho entre a recuperação anterior e a seguinte. Para servir de limite, a maior temperatura registrada naquele instante deve ser menor ou igual ao valor de recuperação da regra. Na ausência de um limite, o cálculo segue até a extremidade correspondente do histórico. Dois IDs no mesmo instante contam uma vez para confirmar a alta, usando a maior temperatura daquele instante. Um reenvio do mesmo ID e conteúdo mantém a medição e o histórico já registrados.

A medição, o estado da sequência, as ocorrências e as lacunas são gravados na mesma transação no SQLite da central, antes da confirmação ao sensor. A central reconstitui as ocorrências das bases 0.1.x ao abri-las pela primeira vez. Esse cálculo usa todas as leituras preservadas, inclusive as que já saíram da tabela das 60 mais recentes do painel. Para atualizar uma instalação, extraia o novo ZIP em outra pasta e indique a pasta de dados existente com `--dados` depois de encerrar a central antiga. O tempo da primeira abertura e do recálculo de dados atrasados depende do tamanho da série.

### Revisão de persistência e concorrência na 0.2.1

A abertura de uma base já atualizada permite consultar as ocorrências enquanto outro processo recalcula alertas. Cada consulta vê as medições, as regras, as lacunas e as ocorrências do mesmo instante de confirmação do banco. Uma duplicata já gravada pode receber seu recibo durante essa escrita. Para um ID ainda desconhecido, o coletor reserva a escrita e confere a identidade novamente. Desse modo, uma confirmação exige que a leitura correspondente já esteja gravada.

Dois IDs diferentes no mesmo horário permanecem na tabela de leituras. Se o segundo valor for igual ou inferior à máxima já conhecida, a central conserva as ocorrências existentes. O gráfico acompanha as máximas dos últimos 60 instantes distintos do sensor apresentado. Os cartões mostram a temperatura e a umidade da mesma leitura que forneceu a máxima mais recente. Em caso de empate entre máximas, o menor ID define a leitura dos cartões. O histórico apresenta as frações de segundo presentes nos dados.

Em um ensaio com 120 instantes já gravados, uma leitura intermediária levou a versão anterior a avaliar 121 instantes e a versão 0.2.1 a avaliar seis. Esse número mede aplicações da regra naquele perfil. O custo de localizar os limites e a extensão do recálculo dependem das temperaturas do sensor. Alterar a regra continua recalculando todo o histórico daquele sensor.

A suíte usa um cálculo de referência separado, que parte das medições e procura sequências de três altas em cada trecho com cobertura. Os testes verificam envios concorrentes do mesmo ID, mudanças de regra por dois processos, o painel durante um commit e a recuperação após encerramento abrupto em plena correção do histórico. A migração inicial também confirma estruturas e dados derivados em uma única transação. O projeto foi desenvolvido para o disco local. [Isolamento de transações no SQLite](https://sqlite.org/isolation.html)

## Experimentar no Windows

1. Extraia **todo** o ZIP em uma pasta onde você possa gravar arquivos, como `D:\Projetos\TelemetriaLocal`.
2. Instale Python **3.12 ou 3.13** com `pip` e suporte a ambientes virtuais. O instalador oficial do Windows oferece esses componentes. Se o Python já estiver instalado, passe ao próximo passo. [Downloads do Python](https://www.python.org/downloads/)
3. No terminal da pasta extraída, rode a demonstração e depois abra o arquivo `relatorio.html` indicado no final.

~~~powershell
.\iniciar.cmd demo
~~~

A primeira execução usa internet para instalar as versões de dependências listadas em `requirements.txt`. O iniciador cria `.venv` para o Python do projeto e `.cache` para downloads e temporários. Depois, demonstração, painel e testes funcionam offline. Python é a única instalação necessária. O próprio projeto inicia o serviço MQTT. SQLite acompanha o Python e o painel abre no navegador que você já usa.

Se o Python estiver em uma pasta personalizada, é possível indicar o executável antes da primeira execução.

~~~powershell
$env:TELEMETRIA_PYTHON = 'D:\caminho\do\python.exe'
.\iniciar.cmd demo
~~~

## Usar o painel ao vivo

~~~powershell
.\iniciar.cmd
~~~

Abra `http://127.0.0.1:8765` e mantenha o terminal aberto. O sensor mede a cada dois segundos e a página se atualiza a cada cinco. O painel mostra temperatura, umidade, total de leituras únicas e tempo desde o último recebimento. A seção **Comunicação dos sensores** mostra a última presença de cada sensor, os períodos sem observação da central e o histórico de interrupções e retomadas. A seção **Ocorrências de temperatura** reúne situação, início, fim ou última observação, duração e pico. A tabela de leituras distingue **medido em** de **recebido em**. Os horários usam UTC. O gráfico mostra o sensor da medição mais recente e a tabela contém os últimos 60 registros de todos os sensores. Ctrl+C encerra a execução.

Para ver a queda acontecer com o sensor em funcionamento, use dois terminais na pasta do projeto.

| Terminal | Comando Windows | O que observar |
| --- | --- | --- |
| 1 | `.\iniciar.cmd central` | Inicia o intermediário MQTT, o coletor e o painel. |
| 2 | `.\iniciar.cmd sensor` | O sensor começa a medir e tenta entregar automaticamente. |

Pare **apenas o terminal 1** com Ctrl+C. Deixe o terminal 2 rodando por vinte segundos. Reinicie a central com o mesmo comando e abra o painel. As medições feitas durante a queda terão seus horários de medição separados por aproximadamente dois segundos, com horários de recebimento agrupados após a reconexão.

Para escolher as portas, use, por exemplo, `.\iniciar.cmd iniciar --mqtt 1884 --web 8766`. Em terminais separados, passe a mesma porta MQTT aos dois comandos. As opções `--dados`, `--sensor` e `--intervalo` permitem escolher a pasta de dados, o identificador do sensor e os segundos entre medições. Consulte `.\iniciar.cmd --help`.

## Linux e macOS

Use Python 3.12 ou 3.13 com `venv` e `pip`. Em distribuições que distribuem `venv` separadamente, instale o pacote correspondente à versão do Python. Os mesmos modos funcionam com o iniciador de shell.

~~~sh
sh iniciar.sh demo
sh iniciar.sh testar
sh iniciar.sh iniciar
~~~

## Como os dados chegam ao painel

| Etapa | Responsabilidade |
| --- | --- |
| Sensor Python | Cria uma identidade por medição e registra horário UTC, temperatura em °C e umidade relativa em %. Salva a leitura em `sensor.sqlite3` antes de tentar enviá-la. |
| MQTT local | Um processo pode hospedar o intermediário e o coletor. O sensor conecta a `127.0.0.1` por TCP e publica no tópico `telemetria/v1/<sensor>/leituras`. Na versão inicial, o intermediário é a biblioteca Python mqttools. [Documentação do MQTT Tools](https://mqttools.readthedocs.io/en/latest/) |
| Coletor | Valida os campos e o tópico e grava em `central.sqlite3`. A chave primária é o par **sensor e ID da medição**. Repetir o mesmo conteúdo devolve uma confirmação e preserva o registro original. Se o mesmo ID chegar com outros valores, o coletor recusa a mensagem e registra o motivo no terminal. |
| Confirmação e reenvio | Depois do commit no SQLite, o coletor publica o recibo em `telemetria/v1/<sensor>/recibos`. O sensor confere o tópico, o ID e o resumo SHA-256 do conteúdo antes de remover a pendência. Enquanto a central estiver indisponível, ele continua medindo. As tentativas de conexão têm espera progressiva de até oito segundos. |
| Painel e relatório | O navegador consulta o histórico local. A demonstração também gera uma página HTML independente que pode ser aberta depois de encerrar o programa. |

O transporte usa **MQTT com QoS 0**. A confirmação relevante é a gravação pelo coletor. Se a central gravar e a resposta se perder, o sensor ainda possui a leitura e pode enviá-la novamente. O SQLite impede que essa segunda entrega crie outra linha. O SHA-256 associa o recibo ao conteúdo e a comunicação foi projetada para processos confiáveis na mesma máquina.

## Reproduzir o resultado e executar os testes

~~~powershell
.\iniciar.cmd demo
.\iniciar.cmd testar
~~~

| Momento da demonstração | Pendentes no sensor | Leituras únicas na central |
| --- | ---: | ---: |
| Dez medições feitas com a central desligada | 10 | 0 |
| Reconexão e confirmação pelo coletor | 0 | 10 |
| Reenvio explícito da primeira medição | 0 | 10 |

Cada demonstração usa uma nova subpasta em `dados/demonstracoes/`. O ensaio verifica o conjunto de IDs **e** de horários originais e informa o caminho do relatório. O modo ao vivo mantém seus dois bancos em `dados/`. Encerrar e iniciar o programa preserva o histórico e a fila. Para começar outro ensaio ao vivo, indique uma pasta nova com `--dados`.

Os testes do monitor verificam o limite de 15 segundos, a separação por sensor, as respostas repetidas e vencidas, o reenvio de medições antigas, a parada e a retomada da central, a persistência do histórico e a demonstração por MQTT com dois sensores.

Os testes de alertas comparam o histórico cronológico com envios atrasados e repetidos, exercitam mudanças de regra, reinício e lacunas e verificam que falhas de gravação preservam a última transação completa.

A suíte usa a biblioteca padrão `unittest`, arquivos isolados em `.cache/testes/` e portas locais temporárias. Ela cobre queda e retorno da conexão, medições durante a queda, reabertura dos dois bancos, perda de confirmação após a gravação, recibos inválidos, duplicatas simultâneas, conteúdo conflitante, isolamento por sensor e leitura do painel por HTTP.

## Persistência e concorrência na versão 0.1.1

A identidade de uma medição é o par formado por sensor e ID. Repetir a inclusão da mesma leitura pendente é seguro mesmo com dois processos. Se dois remetentes enviarem a mesma pendência, o coletor guarda uma linha e apenas o remetente que efetivamente a removeu conta a baixa. A fila e o histórico usam transações SQLite em WAL com sincronização `FULL`.

Quando a central já guarda outro conteúdo para o mesmo ID, ela preserva o registro original e responde com o identificador e o SHA-256 do conteúdo que recusou. O sensor mantém a medição original na fila local e grava uma marca que a exclui dos próximos lotes. As medições seguintes continuam sendo enviadas. No Windows, este comando mostra os 50 conflitos mais recentes do sensor escolhido.

~~~powershell
.\iniciar.cmd conflitos --sensor sala-01
~~~

Use `--dados` para apontar para a pasta do sensor que deseja consultar. Em Linux/macOS, use `sh iniciar.sh conflitos --sensor sala-01`. A versão 0.1.1 abre bancos criados pela 0.1.0 e acrescenta a tabela de marcas na fila. Use a versão atual nos dois lados para obter o tratamento de conflitos.

Se outro processo estiver escrevendo, o serviço aguarda o SQLite e tenta novamente a mesma operação fora do ciclo que atende a rede. Um bloqueio prolongado no banco **do sensor** pode atrasar a próxima amostragem. A recuperação cobre medições já gravadas em disco. Uma medição que ainda está na memória depende de o processo conseguir concluir a gravação. A simulação cobre saída abrupta do processo, e a durabilidade física também depende do disco e do sistema operacional. [Documentação do SQLite sobre sincronização](https://www.sqlite.org/pragma.html#pragma_synchronous).

Os testes da revisão também exercitam dois processos gravando a mesma medição, dois remetentes disputando um lote de 50, interrupção no meio de um lote, retomada da assinatura MQTT do coletor e reabertura do banco após saída abrupta com uma transação ainda aberta.

## Escopo da primeira versão

Esta entrega é um laboratório local com dados simulados e espaço em disco como limite para o histórico e a fila. MQTT e HTTP escutam apenas em `127.0.0.1`. Um piloto com ESP32 físico em outra máquina exigirá configuração de rede, autenticação dos dispositivos e proteção do transporte. A versão atual pode ser experimentada sem hardware, cartão, conta em nuvem ou extensão do VS Code.
