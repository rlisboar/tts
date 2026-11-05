# Marca d'água no caminho remoto (#812)

O #797 fechou as famílias isoladas e declarou a remota fora de escopo. O card
seguinte pediu para fechar também o caminho CUDA.

## O que já estava certo

O caminho remoto **compartilha o laço** do job com o local: o trecho que volta de
`_tts_remote_chunk` cai no mesmo `sf.write` e no mesmo bloco de marca, logo em
seguida. Ou seja, o cliente já marcava — o que faltava era isso estar **provado**,
porque o `remote/omni_server.py` não tem marca d'água nenhuma (só `sf.write` no
buffer) e a garantia vivia na leitura do código.

## O que ficou coberto

| célula | o que segura |
|---|---|
| `test_retorno_remoto_sai_marcado` | peça vinda do servidor com detecção >0,9 |
| `test_fallback_para_local_marca_uma_vez_so` | `remote_fallback` (#114): cai no local e marca **uma vez por peça**, não duas |
| `test_remoto_com_a_marca_desligada_nao_marca` | setting desligada não marca (controle negativo) |

Mordida: desligando o bloco de marca, as duas primeiras caem.

## A contagem que salva o teste

`test_fallback_para_local_marca_uma_vez_so` conta **chamadas de `marca_wav`**, não
probabilidade: marca dupla continua dando detecção 1,0, então olhar só a detecção
aprovava o defeito. Fica registrado porque é a armadilha que o próximo leitor
repete.

## Limite declarado

O servidor continua sem marcar. Quem consumir o `remote/omni_server.py` **direto**
(não pelo app) recebe áudio cru — a marca é contrato do cliente, e é assim que o
card pediu ("não confie no servidor"). Amostra de voz criada por *voice design* com
o remoto ligado também não é marcada: é referência, não saída.
