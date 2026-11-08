"""#876 — transcrições do MLS PT (dev/test) por arquivo extraído. Lê só dados (pyarrow)."""
import json, sys, pathlib
import pyarrow.parquet as pq
dl, wd = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
meta = json.loads((wd / "meta.json").read_text()); refs = {}
for split in ("dev", "test"):
    t = pq.read_table(dl / f"{split}.parquet", columns=["id", "speaker_id", "transcript"]).to_pydict()
    por = {f"{split}_{s}_{i}.wav": tx for i, s, tx in zip(t["id"], t["speaker_id"], t["transcript"])}
    refs.update({k: por[k] for k in meta if k in por})
(wd / "refs.json").write_text(json.dumps(refs, ensure_ascii=False))
print(len(refs))
