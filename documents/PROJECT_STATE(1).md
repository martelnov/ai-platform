# AI Platform — aktuální stav a předání práce

**Stav k:** 5. listopadu 2026  
**Repozitář:** https://github.com/martelnov/ai-platform (veřejná větev `main`)  
**Poslední zkontrolovaný commit:** `a2a09ae`  
**Předchozí dokumentovaný checkpoint:** `1e629e7` (3. listopadu 2026)  
**Pracovní adresář na testovacím serveru:** `/AI/apps/ai-platform`  
**Související dokument:** [ARCHITECTURE.md](ARCHITECTURE.md)

> **Důležité rozlišení:** „V repozitáři“ ≠ „nasazeno na serveru“ ≠ „otestováno“. Tento dokument vychází z veřejného zdrojového kódu a z terminálových výsledků předaných během konverzace do 3. 11. 2026. Novější kód byl zkontrolován, ale aktuální běh na `ailab` nebyl znovu ověřen. Dokument není automatický monitoring.

## 1. Shrnutí jednou stránkou

AI Platform již obsahuje **centrální Python/FastAPI Control Plane, PostgreSQL, Python Gateway API, privilegovaný host-agent, správu Linux uživatelů a adresářů, framework pro mounty a fscrypt**. Databáze obsahuje také základní schéma pro plánování GPU a kontejnerové workloady. Host-agent umí vytvořit **persistentní uživatelský Tailscale konektor** `ai-ts-<id>`.

Od posledního checkpointu přibyl **lehký uživatelský rozcestník v Pythonu** (`ai-launcher-<id>`), jeho kontejnerový obraz, automatický launcher manager a systemd služba s timerem pro pravidelnou kontrolu launcherů. Launcher sdílí síťový namespace s příslušným `ai-ts-<id>` a má poskytovat uživateli trvale dostupný web přes Tailscale Serve. Základní zobrazení identity a služeb je v kódu; ovládání aplikací, šifrování a GPU rezervací v rozcestníku zatím hotové není.

Do veřejné větve `main` byla také přidána **migrace `011_allow_encryption_under_mount.sql`**, která umožňuje definovat fscrypt root uvnitř již existujícího uživatelského mountu. **Aplikování migrace v PostgreSQL ani její integrační testy na `ailab` nejsou aktuálními výpisy potvrzeny.**

Plnohodnotný on-demand runtime pro ComfyUI, Ollamu/Open WebUI, ACE-Step a správce souborů, atomické přidělování 0/1/N GPU a společná CPU kvóta pro AI workloady bez GPU rezervace zůstávají dalšími fázemi.

### Změny od checkpointu 3. listopadu

| Commit | Obsah změny | Co lze potvrdit |
| --- | --- | --- |
| `e2b97e1` | `011_allow_encryption_under_mount.sql` — asymetrická kontrola překryvů fscrypt rootů a mountů | Soubor a SQL implementace jsou v Gitu. |
| `827173a` | `gateway/launcher.py`, `gateway/Dockerfile.python`, úprava `gateway/index.html` | Python rozcestník a základ HTML jsou v Gitu. |
| `a2a09ae` | Launcher manager, integrace s host-agentem, systemd service a timer | Orchestrace launcherů je v Gitu. |

**Zásada pro další práci:** nejdřív zjistit skutečný stav serveru, teprve potom provádět zásahy. Samotný commit neprokazuje nasazení.

## 2. Stav podle komponent

| Oblast | Stav | Co je doloženo / co chybí |
| --- | --- | --- |
| Struktura projektu | **V Gitu** | `compose.yml`, `control-plane/`, `db/migrations/`, `host/`, `gateway/`, `install/`, `templates/`. |
| PostgreSQL | **V Gitu; dříve testováno** | `postgres:18-bookworm`, databáze `ai_platform`, Compose a soubor se secretem. Aktuální běh ověřit. |
| Control Plane | **V Gitu; dříve testováno** | FastAPI, administrace, identita přes Tailscale Serve, uživatelé, secrets, mounty a definice encryptable rootů. Aktuální běh ověřit. |
| Gateway API | **V Gitu; fscrypt API dříve testováno** | `/health`, `/v1/me`, `/v1/storage`, fscrypt operace. Není hotová univerzální aplikační proxy/orchestrace. |
| Host-agent | **V Gitu; dříve testováno** | Python root služba na `/run/ai-platform/agent.sock`; mounty, fscrypt, provisioning Tailscale; nově pokus o vytvoření launcheru. Hodnota `ping.version` je stále `13`, a proto sama nedokazuje nasazenou revizi. |
| Linux UID/GID a uživatelské adresáře | **Implementace + dřívější testy** | `host/ai-platform-helper.py`; DATA a MODELS; provisioning testovacího uživatele byl ověřován. |
| Nativní FS quotas | **Schéma + helper** | `filesystem_quotas`; helper umí `setquota` a stav. Automatizaci ověřit integračně. |
| Mounty | **Schéma + backend; dílčí testy** | `user_mounts`, agent `apply/remove/status`, admin UI. Doplnit regresi pravidel a chybových stavů. |
| fscrypt | **Backend dříve testován** | Přes Gateway API ověřeny `status`, `enable`, `lock`, `unlock`, `change-passphrase`. Uživatelské UI zatím nedokončeno. |
| Migrace 011 | **V Gitu; nasazení neověřeno** | `011_allow_encryption_under_mount.sql` existuje v `main`. Ověřit `schema_migrations` v DB a reálný fscrypt pod mountem. |
| Tailscale `ai-ts-<id>` | **Provisioning v kódu** | Trvalý kontejner `tailscale/tailscale`, persistentní state, `unless-stopped`; auth key se po provisioningu odstraní. Aktuální kontejnery ověřit. |
| Python launcher `ai-launcher-<id>` | **V Gitu; běh neověřen** | `launcher.py`, HTML, obraz `ai-platform-launcher:latest`; `/`, `/health`, `/api/me`. |
| Launcher manager | **V Gitu; běh neověřen** | `host/ai-platform-launcher-manager.py`; kontroluje `ai-ts-<id>`, vytváří/obnovuje launcher a nastavuje Tailscale Serve. |
| Reconciliation launcherů | **V Gitu; aktivace neověřena** | `ai-platform-launchers.service` a `.timer`, přibližně dvouminutový interval po dokončení předchozí kontroly. |
| Uživatelský rozcestník – akce | **Pouze připravené sekce v HTML** | Zobrazení identity existuje; start/stop aplikací, šifrování a rezervace jsou zatím informační karty. |
| Runtime aplikací | **Cíl / dosud nehotovo** | ComfyUI, Ollama, ACE-Step a file manager on-demand, API proxy, CPU-only/GPU režimy. |
| GPU scheduler | **DB návrh; runtime zbývá** | `gpu_devices`, `reservations.gpu_count`, `workloads`, `workload_gpus`; chybí ověřený plánovač s atomickou alokací. |

## 3. Kód a jeho odpovědnosti

```text
compose.yml                                PostgreSQL, control-plane, gateway-api, Docker sítě
control-plane/app/main.py                 administrátorské webové UI/API
control-plane/app/gateway_api.py          identita konektoru, uživatelské a fscrypt API
control-plane/app/db.py                   připojení k PostgreSQL
host/ai-platform-agent.py                privilegovaný Unix-socket agent (root)
host/ai-platform-helper.py               provisioning Linux adresářů a FS quotas
host/ai-platform-launcher-manager.py     správa kontejnerů ai-launcher-<id> a Tailscale Serve
db/migrations/001_base.sql               základ: uživatelé, aplikace, GPU, storage, konektory
db/migrations/002_runtime.sql            reservations, workloads, workload_gpus
db/migrations/003...010...               provisioning, identity, secrets, mounty, fscrypt
db/migrations/011_allow_encryption_under_mount.sql  změna pravidel overlap fscrypt/mount
gateway/launcher.py                      lehký Python HTTP server rozcestníku
gateway/Dockerfile.python                obraz Python launcheru (python:3.12-alpine)
gateway/index.html                       aktuální HTML rozcestník a zobrazení identity
gateway/main.go                          historický Go prototyp
gateway/Dockerfile                      Dockerfile historického Go prototypu
install/systemd/ai-platform-launchers.service  jednorázové spuštění manageru --all
install/systemd/ai-platform-launchers.timer    periodická reconciliation
install/configure-tailscale-serve.sh    Tailscale Serve pro centrální Control Plane
```

`compose.yml` publikuje Control Plane pouze na `127.0.0.1:8080`. PostgreSQL není přímo publikováno. Sítě jsou `ai-control` (internal), `ai-frontend` a `ai-gateway` (internal). `gateway-api` je samostatná Python služba a komunikuje s host-agentem přes připojený Unix socket. Produkční secrets jsou mimo repozitář v `/etc/ai-platform/secrets/`.

**Důležité oddělení:** `install/configure-tailscale-serve.sh` obsluhuje hostitelský Control Plane. Tailscale Serve pro každého uživatelského konektoru konfiguruje `ai-platform-launcher-manager.py`.

**Launcher není službou v `compose.yml`.** Obraz `ai-platform-launcher:latest` se sestavuje samostatně a kontejnery vytváří manager na hostiteli. Pro existující běžící launcher se nyní kontroluje síťový namespace, identita a URL centrálního API, **nikoli digest použitého image**. Samotné sestavení nové verze obrazu proto nezaručuje aktualizaci běžících launcherů.

### 3.1 Python launcher

- `gateway/launcher.py` používá `ThreadingHTTPServer` ze standardní knihovny Pythonu (bez FastAPI).
- `GET /` vrací `gateway/index.html`.
- `GET /health` vrací jednoduchou odpověď pro kontrolu dostupnosti.
- `GET /api/me` serverově volá centrální Gateway API `/v1/me` a předává `X-AI-Connector-ID` z prostředí.
- `AI_CONNECTOR_PROVIDER_ID` dodává manager při vytváření kontejneru; neposílá ji prohlížeč.
- HTML zobrazuje uživatele a Tailscale konektor a informační karty: správce souborů, ComfyUI, Ollama/Open WebUI, ACE-Step, šifrované prostory, GPU rezervace.
- Server naslouchá pouze na `127.0.0.1:8080` uvnitř sdíleného síťového namespace.

### 3.2 Automatická správa launcherů

Manager při zajišťování `ai-launcher-<id>`:

1. Ověří existenci a běh `ai-ts-<id>` a stav Tailscale `Running`.
2. Získá identitu zařízení z `tailscale status --json` (`Self.ID`).
3. Případně připojí Tailscale kontejner k Docker síti `ai-gateway`.
4. Ověří existující launcher (síťový namespace, provider ID, adresa Gateway API a běh).
5. Podle potřeby vytvoří/obnoví launcher z `ai-platform-launcher:latest`.
6. Ověří nebo nastaví Tailscale Serve na HTTPS → `http://127.0.0.1:8080`.

Vytvořený launcher používá `--network container:ai-ts-<id>`, `--user 65532:65532`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--restart no`; nemá Docker socket ani socket host-agenta.

**Bezpečné chování při konfiguraci Serve:** pokud manager najde neočekávanou neprázdnou konfiguraci, nepřepíše ji naslepo, ale skončí chybou.

Při provisioningu Tailscale konektoru spouští manager host-agent. Neúspěch launcheru nevynutí odstranění již zřízeného konektoru; host-agent vrací upozornění `launcher_warning`.

Jednotka `ai-platform-launchers.service` spouští manager s `--all`. Timer je nastaven na `OnBootSec=1min`, `OnUnitInactiveSec=2min`, `AccuracySec=15s`. Kontrola `--all` prochází **běžící** kontejnery `ai-ts-<id>`; neobnovuje zastavené Tailscale konektory.

## 4. DB: migrace a význam

V kontrolované větvi `main` jsou migrace `001_base.sql` až `011_allow_encryption_under_mount.sql`:

- `001_base`: `users`, `hosts`, `applications`, `application_profiles`, `gpu_devices`, `storage_resources`, `filesystem_quotas`, `user_connectors` a další entity.
- `002_runtime`: `reservations` s `gpu_count`, `workloads` s režimy `preparation`/`gpu`, `workload_gpus`.
- `003`–`007`: provisioning, pravidla username, šifrované platform secrets, admin a identita Tailscale konektoru.
- `008_user_mounts`, `009_user_mounts_extensions_only`: přidělené mounty a pravidla kořenových cest.
- `010_user_encryption_roots`: správa encryptable rootů a původní přísná pravidla překryvů.
- `011_allow_encryption_under_mount`: předefinuje `check_user_encryption_root_overlap()` a zapisuje verzi 11 do `schema_migrations`.

### 4.1 Co mění migrace 011

**Povoluje** šifrovaný root uvnitř již existujícího povoleného mountu, např. mount `/private` a encryption root `/private/secret` ve stejném uživatelském stromu.

**Nadále odmítá:**

- shodnou cestu mountu a encryption rootu;
- mount uvnitř encryption rootu;
- překrytí nebo shodu dvou aktivních encryption rootů;
- dodatečný kolidující mount podle existující opačné kontroly.

Migrace nepřepisuje historii `010`. Před jejím použitím porovnat obsah souboru na serveru, Git a tabulku `schema_migrations`.

Při `enable` musí host-agent ověřovat skutečný backing filesystem a jeho podporu/setup fscrypt. Základní `/AI/models` se fscrypt nezapíná kvůli sdílení a deduplikaci modelů, ale vhodný připojený filesystem **uvnitř** MODELS může šifrování podporovat.

**Stav 011:** je v Gitu, ale **není potvrzeno její aplikování do testovací DB ani regresní testy**.

## 5. Co už bylo skutečně otestováno

Z terminálové historie do **3. listopadu 2026**:

- `ai-platform-agent` odpovídal verzí `13` (po úpravách změny hesla).
- Gateway API obsloužilo zkušební fscrypt root uživatele `test` pod `/AI/data/users/test/private2`.
- Úspěšně byly vyzkoušeny `lock` a `unlock`.
- Po `change-passphrase` se staré heslo odmítlo a nové fungovalo.
- Změny Gateway API pro změnu hesla byly commitnuty a pushnuty (`ca505d7` podle posledního doloženého výpisu).
- `/AI/models` byl zjištěn jako filesystem bez setupu fscrypt; to bylo záměrně ponecháno kvůli sdílení/deduplikaci modelů.

**Od checkpointu 3. 11. máme ověřenou existenci nového kódu v Gitu, nikoli runtime testy** následujících částí:

- migrace 011 přímo v databázi a fscrypt pod mountem;
- sestavení/nasazení Python launcher image;
- HTTPS přístup k `ai-launcher-<id>` přes Tailscale;
- úspěšná odpověď `/api/me` ze skutečného uživatelského zařízení;
- instalace/aktivace systemd timeru a obnova launcheru;
- bezpečné oddělení identit různých uživatelských konektorů.

Testovací hesla, skutečné hodnoty secrets, tokeny a osobní Tailscale identifikátory **do dokumentace ani Git historie neukládat**.

## 6. Dohodnuté chování a hranice implementace

1. **Konektor:** trvale běží jeden lehký `ai-ts-<id>` na uživatele, nezávisle na GPU rezervaci; bez Docker socketu.
2. **Rozcestník:** technologie je rozhodnuta — malý Python HTTP server v samostatném `ai-launcher-<id>`, který sdílí síťový namespace s Tailscale konektorem. Základ v Gitu existuje.
3. **Uživatelské funkce rozcestníku:** správa šifrování a rezervační kalendář se mají řešit přes centrální API; jejich ovládací UI je stále plán.
4. **Ostatní služby:** file manager, ComfyUI, Ollama/Open WebUI a ACE-Step se spouštějí **ručně on-demand**, nikoli trvale.
5. **Ollama API:** dostupné na stabilní adrese po ručním startu; příchozí volání nesmí spustit vypnutou službu. Zachovat nativní i OpenAI-kompatibilní endpointy a streamování.
6. **Bez rezervace:** AI workload nemá GPU; náročné AI kontejnery sdílejí **jediný společný** nízký CPU fond (počáteční návrh ekvivalentu 1 logického CPU celkem). Ovládání platformy a správce souborů mají prostředky oddělené.
7. **S rezervací:** 1 konkrétní GPU, případně 2 nebo obecně N GPU atomicky pro jeden workload. Bez rezervace není možné GPU obejít.
8. **Jeden kontejner aplikace:** neoddělovat Comfy editor a GPU worker; při změně režimu může být nutný bezpečný recreate, data zůstávají persistentní.
9. **Storage:** mounty připravuje host-agent; admin přiděluje lokální zdroje, běžný uživatel má jen povolené operace a případné síťové zdroje.
10. **Technologie:** vlastní řídicí kód sjednotit na Pythonu. Starý `gateway/main.go` je prototyp a bez kontroly nasazení se nemaže.

Podrobný návrh: [ARCHITECTURE.md](ARCHITECTURE.md).

## 7. Otevřené úkoly v doporučeném pořadí

| Pořadí | Úkol | Stav |
| --- | --- | --- |
| **1** | Ověřit aktuální `git status`, revizi na `ailab` a aplikaci migrace 011 v PostgreSQL. | neověřeno |
| **2** | Regresně otestovat pravidla překryvů a fscrypt uvnitř připojeného podporovaného filesystemu. | neověřeno |
| **3** | Ověřit sestavení a nasazení `ai-platform-launcher:latest`. | neověřeno |
| **4** | Ověřit `ai-ts-<id>`, `ai-launcher-<id>`, Tailscale Serve a endpointy `/`, `/health`, `/api/me`. | neověřeno |
| **5** | Ověřit instalaci a aktivaci `ai-platform-launchers.timer` a obnovu launcheru po pádu/restartu. | neověřeno |
| **6** | Zabezpečit důvěryhodné ověření identity volajícího vůči Gateway API, ne pouze hlavičku `X-AI-Connector-ID`. | **nutné před širším nasazením** |
| **7** | Doplnit ovládání fscrypt v uživatelském rozcestníku. | plán |
| **8** | Implementovat on-demand start/stop aplikačních kontejnerů. | plán |
| **9** | Integrovat správce souborů a později správu modelů. | plán |
| **10** | Zpřístupnit stabilní Ollama API přes konektor, včetně streamování a režimu bez auto-startu. | plán |
| **11** | Implementovat GPU rezervace, atomické přidělování 0/1/N GPU a per-user runtime. | plán |
| **12** | Implementovat sdílený CPU fond pro AI kontejnery bez GPU rezervace. | plán |
| **13** | Dodat regresní a bezpečnostní testy mountů, fscrypt, oprávnění, kvót, izolace a cleanup. | průběžně |
| **14** | VeraCrypt vault a jeho přenos/synchronizace. | později |

### Bezpečnostní oblasti před nasazením dalším uživatelům

- Zajistit, aby klient nemohl podvrhnout `Tailscale-User-*` hlavičky předávané Control Plane mimo důvěryhodný Tailscale Serve.
- `gateway_api.py` nyní identifikuje konektor podle `X-AI-Connector-ID`. Samotná hlavička není nezávislá autentizace: interní kontejner by mohl deklarovat identitu cizího konektoru. Doplnit důvěryhodný mechanismus ještě před privilegovanými akcemi.
- Běžné aplikační kontejnery oddělit od PostgreSQL, Docker socketu, host-agent socketu a cizích dat; sdílení síťového namespace nesmí izolaci obejít.
- U root operací řešit chybové stavy, kdy se změna na hostu provede, ale DB transakce se nepotvrdí.
- Prověřit veřejnou historii Gitu na omylem commitnuté secrets; skrytí/změna viditelnosti repozitáře samo neodstraní historii.
- Vyřešit řízený rollout nové verze launcher image; současný manager neporovnává image digest již běžícího kontejneru.

## 8. Bezpečné první ověřovací příkazy po navázání

Následující příkazy jsou **pouze čtecí**. Výstupy předává správce hostitele; **nikdy neposílat secrets, auth keys ani soukromé Tailscale identifikátory**.

```bash
cd /AI/apps/ai-platform

git status --short
git log -1 --oneline
ls -l db/migrations/011_allow_encryption_under_mount.sql

docker compose --env-file /etc/ai-platform/compose.env -f compose.yml ps

docker ps -a --filter 'name=ai-ts-' \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker ps -a --filter 'name=ai-launcher-' \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

docker image inspect ai-platform-launcher:latest \
    --format '{{.Id}}' 2>/dev/null || true

systemctl status ai-platform-launchers.timer --no-pager
systemctl status ai-platform-launchers.service --no-pager
```

Databázové ověření verzí migrací až po kontrole Compose:

```bash
docker compose --env-file /etc/ai-platform/compose.env -f compose.yml \
    exec -T postgres \
    psql -U ai_platform -d ai_platform \
    -c 'SELECT version, applied_at FROM schema_migrations ORDER BY version;'
```

Případné čtení Tailscale Serve konfigurace, logů a `/api/me` musí před sdílením výstupu odstranit individuální identifikátory uživatelů a zařízení.

**Migrace, sestavení image, aktivace timeru, restart služeb ani jiné změny až po vyhodnocení skutečného stavu — ne naslepo.**

## 9. Pravidla vedení dokumentace a předávání konverzací

- Po dokončení většího celku aktualizovat **[PROJECT_STATE.md](PROJECT_STATE.md)**: co bylo implementováno, nasazeno a skutečně otestováno, s uvedením commitu.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** měnit při změně dohodnutých zásad a technické topologie, ne kvůli každému dílčímu testu.
- Při práci vždy rozlišovat veřejný `main`, případné necommitnuté změny na `ailab` a stav běžících služeb.
- Pro další konverzaci stačí odkaz na repozitář, tyto dokumenty a poslední `git status`/relevantní runtime výpisy; není nutné kopírovat celou historii chatu.
- Dokumenty se samy neověřují a nenahrazují aktuální kontrolu serveru.

**Poslední zaznamenaný checkpoint: `a2a09ae` — implementován základ Python launcherů a jejich automatické správy; provozní ověření dosud není doloženo.**
