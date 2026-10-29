# Licenças — conferir antes de promover candidato a dependência

| item | licença do código | licença dos pesos | uso no bake-off |
|---|---|---|---|
| LibriSpeech dev-clean | CC BY 4.0 (corpus) | — | fala pública do corpus |
| FLEURS pt_br (`google/fleurs`) | CC BY 4.0 (corpus) | — | fala pt REAL com 30+ locutores (R1b) |
| Resemblyzer | MIT (código) | MIT (pesos do repo) | linha de base |
| WeSpeaker ResNet34/221 | Apache-2.0 | CC BY 4.0 (ModelScope/HF) | candidato |
| 3D-Speaker CAM++ / ERes2NetV2 | Apache-2.0 | Apache-2.0 | candidato |
| SpeechBrain ECAPA-TDNN | Apache-2.0 | CC BY 4.0 (VoxCeleb) | candidato |
| DeepFilterNet3 | MIT/Apache-2.0 (dupla) | MIT/Apache-2.0 | candidato ruído |
| MossFormer2_SE_48K | Apache-2.0 | Apache-2.0 | candidato ruído |
| MossFormer2_SS_16K | Apache-2.0 | Apache-2.0 (`alibabasglab/MossFormer2_SS_16K`, tag `license:apache-2.0`) | separação alvo × concorrente (#532) — **offline em `models/mossformer2-ss-16k`**, código vendorizado em `tools/separa_vend/` com NOTICE (#526-bis) |
| Resemble-Enhance | MIT | MIT | candidato ruído |
| Demucs (htdemucs) | MIT | MIT | separação música — MEDIDO no import (#520) |
| AudioSeal | MIT | MIT (CC BY-NC 4.0 no pacote meta) | anti-clone |
| AASIST (`clovaai/aasist`) | MIT | MIT — pesos `AASIST.pth`/`AASIST-L.pth` vivem no MESMO repositório MIT (NAVER Corp.); o dataset ASVspoof2019 LA não é herdado para inferência | detector de sintético |
| DNSMOS P.835 | MIT (onnx) | MIT | métrica |

**Pendência declarada:** licença de PESOS de cada um só vale para uso local; antes
de embarcar no app, o item precisa de confirmação de que a licença cobre
distribuição junto do produto. Este arquivo é o registro disso, não a garantia.
