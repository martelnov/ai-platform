# AI Platform — aktuální stav a předání práce

**Stav k:** 9. listopadu 2026
**Repozitář:** https://github.com/martelnov/ai-platform
**Vývojová větev:** `feature/user-provisioning-fscrypt`, commit `ac3b99e` (push potvrzen 9. 11. 2026)
**Související SalamanderRS:** https://github.com/martelnov/SalamanderRS, větev `web-v0.10`, commit `fa51575`
**Pracovní adresář na testovacím serveru:** `/AI/apps/ai-platform`  
**Související dokument:** [ARCHITECTURE.md](ARCHITECTURE.md)

> **Rozlišení stavů:** „v Gitu“ ≠ „nasazeno“ ≠ „integračně otestováno“. Tento dokument vychází z doložených výpisů serveru `ailab` do 9. 11. 2026. Není to průběžný monitoring. Aktuálně popsaná integrace je ve **vývojových větvích**, nikoli automaticky v `main`. Před nasazením na další stroj ověřit DB migrace, sestavení image, secrets a skutečnou konfiguraci hostitele.

## 1. Shrnutí jednou stránkou

AI Platform má **Python/FastAPI Control Plane, PostgreSQL, Gateway API, privilegovaný host-agent, provisioning Linux UID/GID, mounty, fscrypt a persistentní uživatelské Tailscale konektory**. Pro uživatele funguje lehký **statický HTML/JavaScript rozcestník** obsluhovaný Python launcherem `ai-launcher-<id>`.

**SalamanderRS je první skutečně integrovaná aplikace on-demand.** Na `ailab` byl ověřen start, otevření přes Tailscale Serve i stop z rozcestníku. Pro testovací konektor běží rozcestník přes HTTPS 443 a Salamander přes oddělený HTTPS 8443; interně jde o porty 8080 a 8090 ve sdíleném síťovém namespace Tailscale konektoru. Aplikace dostává pouze uživatelské `/data` a `/models`, nikoli data ostatních.

**Identita konektoru je autentizována HMAC-SHA256**, nikoli samotnou hlavičkou `X-AI-Connector-ID`. Gateway ověřuje token a identitu dohledává v databázi; uživatelské rozhraní neposílá libovolné ID uživatele ani workloadu. Kořenový klíč je mimo Git v `/etc/ai-platform/secrets/launcher_auth.key`; per-konektor launcher dostává jen odvozený token.

**Důležitá provozní oprava:** SalamanderRS nově zpracovává SIGTERM s graceful shutdown. Testovací stop z rozcestníku se zkrátil přibližně z 23,59 s na 4 s; Docker potvrdil `exitCode=0` bez SIGKILL. Konkrétní chování při aktivním velkém přenosu ještě není ověřeno.

**Ještě nejsou hotové:** obecné spouštění ComfyUI, Ollamy/Open WebUI a ACE-Step; plánování 0/1/N GPU a společný nízký CPU fond; zcela reprodukovatelné sestavení/registrace nového Salamander image při čisté instalaci. Automatický instalátor z GitHubu je budoucí požadavek, ale nyní se neimplementuje.

**Git k 9. 11. 2026:** AI Platform `ac3b99e` na `feature/user-provisioning-fscrypt`, SalamanderRS `fa51575` na `web-v0.10`. Oba pracovní stromy byly uživatelem doloženy jako čisté a synchronizované s `origin`. Větve `main` nebyly měněny.

## 2. Stav podle komponent

| Oblast | Stav | Doloženo / zbývá |
| --- | --- | --- |
| PostgreSQL | **Používáno v integračním testu** | Z DB se zjišťoval Salamander workload; image v `applications` byla lokálně aktualizována. Plný audit migrací před čistou instalací zbývá. |
| Control Plane | **V kódu; dříve testováno** | FastAPI administrace, uživatelé, identity, secrets, mounty a fscrypt. Aktuální kompletní regresní test UI zbývá. |
| Gateway API | **Nasazeno a testováno** | HMAC ověření konektoru; přes launcher ověřeny `/api/me` (HTTP 200) a `/api/salamander`. Start/stop jsou autorizovány serverově vůči uživateli/workloadu v DB. |
| Host-agent | **Nasazeno a testováno pro Salamander** | Privilegovaný Unix socket, validace spec/UID/GID, docker start/stop a stav workloadu. Stop používá `docker stop --time 20`; Graceful SIGTERM řeší proces SalamanderRS. |
| Linux UID/GID, DATA a MODELS | **Implementováno; otestováno dílčím způsobem** | Per-user prostory `/AI/data/users/<username>` a `/AI/models/users/<username>`; v Salamander připojeny jako `/data`, `/models`. |
| Native filesystem quotas | **Schéma + helper** | `filesystem_quotas` a host helper; automatické vynucení ve všech scénářích neprokázáno. |
| Mounty | **Schéma a backend; dílčí testy** | `user_mounts`, agent apply/remove/status. Vyžaduje regresní testy překryvů a chybových stavů. |
| fscrypt | **Backend dříve otestován** | `status`, `enable`, `lock`, `unlock`, `change-passphrase`; stav UI a migrace 011/012 ověřit proti aktuální DB. |
| Tailscale `ai-ts-<id>` | **Nasazeno a testováno** | Persistentní konektory; Tailscale Serve pro rozcestník na 443 a Salamander na 8443; druhý port se nastavuje bez přepsání prvního. |
| Python launcher `ai-launcher-<id>` | **Nasazeno a testováno** | Statický HTML+JS rozcestník; proxy k centrální Gateway; HMAC token každého konektoru; funkční uživatelské akce. Systemd timer `ai-platform-launchers.timer` byl po nasazení obnoven a ověřen jako aktivní. |
| SalamanderRS `ai-files-<id>` | **Nasazeno a testováno pro testovacího uživatele** | Jedna utility aplikace on-demand bez GPU, sdílený network namespace s Tailscale, vlastní DATA/MODELS; Start/Otevřít/Stop funguje. |
| Salamander Docker image | **Lokálně sestaveno; reprodukovatelnost zbývá** | Na `ailab` je použit image `ai-platform-salamander:0.10.0-sigterm`, odvozený z `0.10.0-7804126`. V DB byl tag změněn ručně. Čistý instalátor to dosud nezajistí. |
| ComfyUI, Ollama, ACE-Step | **Plán** | Bez dokončeného on-demand runtime, API proxy, websocket/streaming testů a přidělování GPU. |
| GPU scheduler a sdílená CPU kvóta | **Datový model / návrh** | `gpu_devices`, `reservations`, `workloads`, `workload_gpus` existují; komplexní scheduler a cgroup vynucení dosud neověřeny. |

## 3. Kód a odpovědnosti

```text
compose.yml                            PostgreSQL, Control Plane, gateway-api, sítě a secrets
control-plane/app/main.py             centrální administrace / API
control-plane/app/gateway_api.py      uživatelské API, per-user autorizace, Salamander akce
control-plane/app/connector_auth.py   HMAC-SHA256 ověření identit konektorů
control-plane/app/db.py              PostgreSQL připojení
gateway/index.html                   statický HTML/JS uživatelský rozcestník
gateway/launcher.py                  lehký HTTP launcher a proxy /api/*
host/ai-platform-launcher-manager.py  lifecycle launcherů a Tailscale Serve portů
host/ai-platform-agent.py            privilegovaný host-agent pro Docker, mounty, fscrypt
host/ai-platform-helper.py           provisioning UID/GID, kvóty a stav workloadů
db/migrations/001...013...           databázový model včetně utility Salamander runtime
tests/test_connector_auth.py         HMAC validace
tests/test_salamander_*.py           Gateway, launcher, Serve a runtime testy
gateway/main.go                      starší Go prototyp; nepovažovat za aktuální rozcestník
install/configure-tailscale-serve.sh  Tailscale Serve pro hostitelský Control Plane
```

Pro uživatele se vytváří `ai-ts-<id>` (trvalý Tailscale uzel) a `ai-launcher-<id>` (malý Python HTTP proces). Po ručním startu může existovat `ai-files-<id>` pro SalamanderRS; ten sdílí síťový namespace `ai-ts-<id>`. Tailscale Serve na 443 směruje na `127.0.0.1:8080`, HTTPS 8443 na `127.0.0.1:8090`. Tato lokální TCP spojení jsou uvnitř namespace konektoru; nejde o veřejné vystavení portů hostitele.

Gateway API komunikuje s host-agentem prostřednictvím jeho Unix socketu. Uživatelské aplikace nemají dostat Docker socket, databázová hesla ani host-agent socket. HMAC root secret je mimo repozitář v `/etc/ai-platform/secrets/`. `install/configure-tailscale-serve.sh` patří k **hostitelskému** Control Plane; nastavení Serve pro jednotlivé uživatele spravuje launcher manager.

## 4. Databázové migrace a image

Základní migrace `001`–`010` pokrývají uživatele, aplikace, GPU, rezervace, mounty a fscrypt. Dřívější dokumentace (3. 11.) uváděla `011_allow_encryption_under_mount.sql` jako rozpracovanou; **její aktuální aplikovaný stav spolu s případnou migrací 012 ověřit** v `schema_migrations`, nikoli odhadovat podle souborů.

Novější schéma obsahuje migraci `013_salamander_utility.sql`, která zavádí utility profil/workload pro SalamanderRS. Původně registrovaný image tag byl `ai-platform-salamander:0.10.0-7804126`; na testovacím serveru byl později **ručně** změněn v `applications.image` na `ai-platform-salamander:0.10.0-sigterm`, aby se ověřila oprava SIGTERM.

**Důsledek:** čisté nasazení zatím nemusí použít opravenou binárku. Před dalším serverem je potřeba definovat reprodukovatelný build (ideálně z konkrétního Git commitu SalamanderRS) a idempotentní novou migraci či konfigurační postup pro aktuální image. **Neměnit zpětně již aplikovanou migraci `013` ani `010`.**

Záměr fscrypt pravidel zůstává: encryption root může ležet *uvnitř předem existujícího mountu* se skutečnou podporou fscrypt; nesmí být totožný s mount bodem ani dovolit opačný nebezpečný překryv. Stav a testy tohoto pravidla na současné DB ještě ověřit.

## 5. Doložené integrační testy

### Dříve ověřené storage a fscrypt

- Gateway API obsloužilo testovací fscrypt root `/AI/data/users/test/private2`.
- `lock` a `unlock` fungovaly; po změně hesla se staré odmítlo a nové přijalo.
- Základní `/AI/models` zůstal bez fscrypt setupu kvůli zamýšlenému sdílení/deduplikaci modelů.

### SalamanderRS / Stage 2 (listopad 2026)

- **34 testů AI Platform** úspěšných; kontrola `git diff --check`, Python compile a `docker compose config -q` také prošly.
- HMAC tokeny tří launcherů byly lokálně ověřeny jako platné; `/api/me` odpovědělo HTTP 200, `/api/salamander` vrátilo registrovaný workload a stav.
- Z prohlížeče byl úspěšně opakovaně otestován **Spustit / Otevřít / Ukončit** u SalamanderRS.
- Naměřeno přibližně **3,9 s start**, původně **23,59 s stop** a po doplnění Tokio graceful SIGTERM přibližně **4 s stop**.
- Docker eventy při novém stop: `SIGTERM`, `exitCode=0`, `destroy`; neobjevil se `SIGKILL`. Izolovaný test kontejneru trval zhruba 2,4 s.
- **27 testů SalamanderRS** a release build prošly před synchronizací/rebase větve. Po rebase na `7804126` vznikl commit `fa51575`; **kompletní rebuild a retest nového HEAD dosud nejsou v doložených výpisech**.
- Zkontrolován Git push a čisté pracovní stromy obou vývojových větví k 9. 11. 2026.

**Ještě netestováno:** graceful shutdown během dlouhého zápisu/obnovitelného přenosu, možná reakce klienta a konzistence souboru; čistá instalace a sestavení přes nový Git HEAD; kompletní izolace v dalších uživatelských kontejnerech.

Do repozitáře, dokumentace ani logů pro sdílení **nepatří tajné klíče, hesla, HMAC tokeny, skutečné hodnoty secrets ani soukromé identifikátory Tailscale**.

## 6. Dohodnuté chování a stav implementace

1. **Persistentní Tailscale identita** `ai-ts-<id>` bez vazby na GPU rezervaci — **implementováno**.
2. **Stále dostupný lehký rozcestník**: statické HTML/JS + Python `ai-launcher-<id>` — **implementováno a ověřeno**.
3. **Bezpečná identita konektoru:** centrální Gateway vyžaduje platný HMAC token odvozený pro konkrétní `provider_id`; ID uživatele/workloadu se určuje na serveru — **implementováno pro aktuální cestu**. Zbývá širší bezpečnostní audit.
4. **Správce souborů:** SalamanderRS on-demand, bez GPU, s vlastními `/data` a `/models`, Tailscale HTTPS 8443 — **otestováno**.
5. **Správa šifrování a rezervační kalendář:** centrální API bez nového persistentního kontejneru; dokončení a regresní testy UI **zbývají**.
6. **Ollama, ComfyUI a ACE-Step:** ruční start/stop, stabilní URL pouze během běhu, žádný implicitní start při API volání; zachovat streaming a WebSockety — **plán**.
7. **CPU bez rezervace:** všechny výpočetní AI workloady dohromady v jednom malém fondu (původně ekvivalent 1 logického CPU); utility služby mimo tento fond — **plán**.
8. **GPU rezervace:** přidělení konkrétního 0/1/N GPU s atomickou alokací podle dostupnosti — **plán**.
9. **Persistentní data:** host-agent připravuje mounty; aplikační kontejner lze bezpečně znovu vytvořit bez ztráty uložené práce — **u SalamanderRS zčásti implementováno**.
10. **Přenositelnost:** výsledný systém se má dát automaticky instalovat a aktualizovat z GitHubu, bez hardcoded hardware; **požadavek platí, instalátor se zatím nevyvíjí**.

Podrobnosti: [ARCHITECTURE.md](ARCHITECTURE.md).

## 7. Otevřené úkoly v doporučeném pořadí

| Pořadí | Úkol | Stav |
| --- | --- | --- |
| **1** | Rerun `cargo test` a `cargo build --release --locked` pro SalamanderRS **po rebase**, tedy HEAD `fa51575`; zkontrolovat čistý tree. | neověřeno po rebase |
| **2** | Zajistit reprodukovatelný Docker build aktuálního SalamanderRS a soulad image tagu s `applications.image` při nové instalaci; nepozměňovat starou migraci `013`. | otevřeno |
| **3** | Ověřit aktuální `schema_migrations` v DB proti migracím `011`, `012`, `013`; doplnit případnou novou migraci pro image. | otevřeno |
| **4** | Otestovat ukončení SalamanderRS **během velkého obnovitelného přenosu**, reakci klienta a bezpečný restart. | otevřeno |
| **5** | Regresní a bezpečnostní testy: přístupové hranice více uživatelů, HMAC, fscrypt, mounty, kvóty a chybové stavy DB/agent. | částečně |
| **6** | Dokončit a otestovat uživatelské UI pro fscrypt a rezervační kalendář. | otevřeno |
| **7** | Zavést další aplikace on-demand: Ollama/Open WebUI, ComfyUI, ACE-Step; stabilní API, streaming, WebSockety. | plán |
| **8** | Implementovat sdílenou nízkou CPU kvótu a scheduler 0/1/N GPU s rezervacemi. | plán |
| **9** | Po dokončení vývojových komponent připravit **automatickou instalaci a aktualizace z GitHubu** pro nový server. | později, zatím nespouštět |
| **10** | Volitelná podpora VeraCrypt vaultů a synchronizace rozdílů. | později |

### Bezpečnostní oblasti před rozšířením na další uživatele

- Kontrolovat identitu na všech per-user Gateway akcích. HMAC token nyní chrání aktuální connector API; **neznamená to dokončený bezpečnostní audit celé platformy**.
- Ujistit se, že proxy klient nemůže podvrhnout `Tailscale-User-*` hlavičky mimo důvěryhodný Tailscale Serve.
- Nepustit Docker socket, PostgreSQL přístup, agent socket ani hostitelské rooty do neprivilegovaného workloadu.
- Prověřit race conditions a rollback při rozdílném stavu Dockeru, host-agenta a PostgreSQL.
- Vyloučit zpřístupnění cizích uživatelských stromů a nechtěné mounty přes symlinky nebo nesprávné UID/GID.
- Prověřit veřejnou historii Git repozitářů kvůli případným únikům secrets.

## 8. Bezpečné první ověření po navázání

Příkazy v tomto oddílu jsou pouze informativní; nevypisovat hodnoty secrets.

```bash
sudo git -C /AI/apps/ai-platform status --short --branch
git -C /home/administrator/SalamanderRS-git status --short --branch
sudo docker compose --env-file /etc/ai-platform/compose.env -f /AI/apps/ai-platform/compose.yml ps
sudo docker ps -a --filter 'name=ai-ts-' --filter 'name=ai-launcher-' --filter 'name=ai-files-'
```

Pro ověření migrační historie (až po kontrole názvu služby a Compose):

```bash
sudo docker compose --env-file /etc/ai-platform/compose.env -f /AI/apps/ai-platform/compose.yml     exec -T postgres psql -U ai_platform -d ai_platform     -c 'SELECT version, applied_at FROM schema_migrations ORDER BY version;'
```

Při nesouladu migrací, image nebo běžících kontejnerů **nedělat automatický restart ani SQL změny bez další kontroly**.

## 9. Pravidla vedení dokumentace a předávání konverzací

- Po dokončení většího celku aktualizovat **[PROJECT_STATE.md](PROJECT_STATE.md)**: co je implementováno, co bylo otestováno, jakým commitem a co zbývá.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** aktualizovat jen při změně rozhodnutých pravidel nebo zásadního technického návrhu.
- Při práci vždy rozlišovat stav `main`, aktivní vývojové větve, lokální změny na `ailab` a stav běžících služeb.
- Do další konverzace stačí odkázat na repo, tyto dva dokumenty a dodat poslední `git status`/důležité runtime výpisy. Historii chatu není nutné znovu kopírovat.
- Tyto dokumenty **nenahrazují aktuální výpis serveru** a neověřují se samy; aktualizovat je průběžně a při blížícím se limitu konverzace připravit přesný checkpoint.
