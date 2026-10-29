"""把 mac 包解压到一个独立目录，验证知识库与学校名单能独立加载。"""
import pathlib
import shutil
import subprocess
import sys
import zipfile

src = sorted(pathlib.Path("dist").glob("*macOS源码*.zip"))[-1]
root = pathlib.Path("data/_mactest")
if root.exists():
    shutil.rmtree(root, ignore_errors=True)
root.mkdir(parents=True)

zz = zipfile.ZipFile(src)
base = zz.namelist()[0].split("/")[0]
count = 0
for n in zz.namelist():
    if n.endswith(("/",)):
        continue
    rel = n[len(base) + 1:]
    if not rel:
        continue
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with zz.open(n) as f, target.open("wb") as out:
        out.write(f.read())
    count += 1
print(f"解压 {count} 个文件 -> {root}")

code = (
    "import sys; sys.path.insert(0, 'boss_vision');"
    "import kb, schools, screening;"
    "d = kb.load();"
    "print('rules', len(d['rules']));"
    "print('school_csv', schools.LIST_CSV.exists());"
    "print('rules_json', screening.RULES_JSON.exists());"
    "print('kb_dir', kb.KB_DIR.exists())"
)
r = subprocess.run([sys.executable, "-c", code], cwd=root,
                   capture_output=True, text=True, encoding="utf-8")
print("--- 加载结果 ---")
print((r.stdout or "").strip())
if r.returncode != 0:
    print("--- stderr ---")
    print((r.stderr or "").strip()[-500:])
shutil.rmtree(root, ignore_errors=True)
