"""Prints what is inside a gogdb dump (file names + a few sample records) so support for it can be added.

    python tools/inspect_gogdb.py gogdb_2026-09-19.tar.xz

Writes gogdb_sample.txt next to where you run it; attach or paste that file. Reads the archive as a
stream, so it never unpacks the 60 MB dump to disk.
"""
import sys
import tarfile

if len(sys.argv) != 2:
    sys.exit(__doc__)
names, samples = [], {}
with tarfile.open(sys.argv[1], "r|xz") as tar:
    for member in tar:
        names.append((member.name, member.size))
        if member.isfile() and member.name.endswith(".json") and len(samples) < 3 and member.size < 300_000:
            samples[member.name] = tar.extractfile(member).read(3000).decode("utf-8", "replace")
        if len(names) >= 500 and len(samples) >= 3:
            break
lines = [f"{len(names)} entries read (first 60 shown):"] + [f"{n}  ({s} bytes)" for n, s in names[:60]]
for name, text in samples.items():
    lines += ["", f"--- {name} (first 3000 characters)", text]
with open("gogdb_sample.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("Wrote gogdb_sample.txt: attach or paste its contents.")
