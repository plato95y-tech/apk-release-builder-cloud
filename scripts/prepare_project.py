#!/usr/bin/env python3
import os, re, sys, json, shutil, hashlib, zipfile, tempfile, subprocess, pathlib

zip_path, dest = sys.argv[1], pathlib.Path(sys.argv[2])
FALLBACK_GRADLE = "8.13"

def log(m): print(f"[prepare] {m}", flush=True)
def out(k, v):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"{k}={v}\n")

def read(p):
    try: return pathlib.Path(p).read_text(encoding="utf-8", errors="ignore")
    except Exception: return ""

# 1. extract
tmp = pathlib.Path(tempfile.mkdtemp())
with zipfile.ZipFile(zip_path) as z:
    z.extractall(tmp)
shutil.rmtree(tmp / "__MACOSX", ignore_errors=True)

# 2. find project root
def depth(p): return len(p.relative_to(tmp).parts)
cands = [p for p in tmp.rglob("settings.gradle*") if "build" not in p.relative_to(tmp).parts[:-1]]
if not cands:
    cands = [p for p in tmp.rglob("build.gradle*")]
if not cands:
    sys.exit("::error::No settings.gradle or build.gradle found - not an Android/Gradle project")
root = min(cands, key=depth).parent
log(f"project root: {root.relative_to(tmp) or '.'}")
if dest.exists(): shutil.rmtree(dest)
shutil.copytree(root, dest)
root = dest

# 3. gradle wrapper
props = read(root / "gradle/wrapper/gradle-wrapper.properties")
m = re.search(r"gradle-([0-9][0-9.]*(?:-[a-z0-9.]+)?)-(?:bin|all)\.zip", props)
gradle_version = m.group(1) if m else FALLBACK_GRADLE
has_wrapper = (root / "gradlew").exists() and (root / "gradle/wrapper/gradle-wrapper.jar").exists()
if has_wrapper:
    os.chmod(root / "gradlew", 0o755)
log(f"gradle wrapper usable: {has_wrapper}; gradle version: {gradle_version}")
out("use_wrapper", "true" if has_wrapper else "false")
out("gradle_version", gradle_version)

# 4. local.properties + SDK licenses
sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "/usr/local/lib/android/sdk"
(root / "local.properties").write_text(f"sdk.dir={sdk}\n")
subprocess.run(f'yes | "{sdk}/cmdline-tools/latest/bin/sdkmanager" --licenses >/dev/null 2>&1',
               shell=True, executable="/bin/bash")

# 5. gradle.properties
gp = root / "gradle.properties"
if "org.gradle.jvmargs" not in read(gp):
    with open(gp, "a") as f: f.write("\norg.gradle.jvmargs=-Xmx4g -Dfile.encoding=UTF-8\n")

# 6. app modules
gfiles = [p for p in root.rglob("build.gradle*") if "build" not in p.relative_to(root).parts[:-1]]
app_modules = [p for p in gfiles if re.search(r"android[.\-]application|com\.android\.application", read(p)) and "apply false" not in read(p)]
log("app modules: " + ", ".join(str(p.parent.relative_to(root) or ".") for p in app_modules))

# 7. google-services.json
toml = read(root / "gradle/libs.versions.toml")
gs_aliases = [re.sub(r"[-_]", ".", k) for k in re.findall(r'^\s*([\w\-]+)\s*=.*com\.google\.gms\.google-services', toml, re.M)]
def uses_gs(text):
    if re.search(r"com\.google\.gms\.google-services|google[.\-]services", text): return True
    return any(a in text for a in gs_aliases)

def package_names(text):
    base = re.findall(r'applicationId\s*=?\s*["\']([\w.]+)["\']', text)
    if not base:
        base = re.findall(r'namespace\s*=?\s*["\']([\w.]+)["\']', text)
    sufs = re.findall(r'applicationIdSuffix\s*=?\s*["\']([\w.]+)["\']', text)
    names = set(base)
    for b in base:
        names.add(b + ".debug")
        for s in sufs: names.add(b + s)
    return sorted(names) or ["com.example.app"]

def dummy_json(pkgs):
    clients = []
    for p in pkgs:
        h = hashlib.md5(p.encode()).hexdigest()
        clients.append({
            "client_info": {"mobilesdk_app_id": f"1:000000000000:android:{h}",
                            "android_client_info": {"package_name": p}},
            "oauth_client": [],
            "api_key": [{"current_key": "AIzaSyDummyKeyForCiBuildOnly0000000000"}],
            "services": {"appinvite_service": {"other_platform_oauth_client": []}}})
    return {"project_info": {"project_number": "000000000000", "project_id": "dummy-ci-project",
                             "storage_bucket": "dummy-ci-project.appspot.com"},
            "client": clients, "configuration_version": "1"}

secret = os.environ.get("GOOGLE_SERVICES_JSON", "").strip()
for mod in app_modules:
    text = read(mod)
    if not uses_gs(text):
        continue
    mdir = mod.parent
    if any(mdir.rglob("google-services.json")):
        log(f"{mdir.name}: google-services.json found in project")
        continue
    target = mdir / "google-services.json"
    if secret:
        try:
            json.loads(secret); target.write_text(secret, encoding="utf-8")
            log(f"{mdir.name}: google-services.json taken from repo secret")
            continue
        except Exception:
            log("::warning::GOOGLE_SERVICES_JSON secret is not valid JSON - generating dummy")
    pk = package_names(text)
    target.write_text(json.dumps(dummy_json(pk), indent=2))
    log(f"{mdir.name}: google-services.json missing -> generated DUMMY for {pk}")
    print("::warning::Dummy google-services.json generated (Firebase features will not work). Add the GOOGLE_SERVICES_JSON secret for the real one.")

# 8. debug keystore
home_ks = pathlib.Path.home() / ".android/debug.keystore"
home_ks.parent.mkdir(parents=True, exist_ok=True)
if not home_ks.exists():
    subprocess.run(["keytool", "-genkeypair", "-keystore", str(home_ks), "-storepass", "android",
                    "-alias", "androiddebugkey", "-keypass", "android", "-keyalg", "RSA",
                    "-keysize", "2048", "-validity", "10000",
                    "-dname", "CN=Android Debug,O=Android,C=US"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
names = {"debug.keystore"}
for p in gfiles + list(root.glob("*.gradle*")):
    for ref in re.findall(r'[\w./${}\-]*debug[\w\-]*\.(?:keystore|jks)', read(p)):
        names.add(ref.split("/")[-1])
for n in names:
    for d in [root] + [p.parent for p in app_modules]:
        t = d / n
        if not t.exists():
            shutil.copy(home_ks, t)
            log(f"debug keystore missing -> generated {t.relative_to(root)}")
log("done")
