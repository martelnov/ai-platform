# AI Platform — aktuální stav a předání práce

**Stav k:** 3. listopadu 2026  
**Repozitář:** https://github.com/martelnov/ai-platform (veřejná větev `main`)  
**Pracovní adresář na testovacím serveru:** `/AI/apps/ai-platform`  
**Související dokument:** [ARCHITECTURE.md](ARCHITECTURE.md)

> **Důležité rozlišení:** „V repozitáři“ ≠ „nasazeno na serveru“ ≠ „otestováno“. Tento dokument vychází z veřejného zdrojového kódu a z terminálových výsledků předaných během konverzace do 3. 11. 2026. Není to automatický monitoring. U míst označených „ověřit“ chybí aktuální výpis hostitele. Při přechodu do další konverzace na něj lze navázat bez opakovaného zkoumání celé historie.

## 1. Shrnutí jednou stránkou

Platforma už má **centrální Python/FastAPI Control Plane, PostgreSQL, Python Gateway API, privilegovaný host-agent, zřizování uživatelských adresářů, framework pro mounty a fscrypt**. V databázi existují tabulky pro budoucí plánování GPU a kontejnerové workloady. Host-agent umí vytvořit **persistentní Tailscale konektor** `ai-ts-<id>`.

**Není ještě hotové jednotné uživatelské rozhraní `ai-ts-<id>` ani obecný runtime pro spouštění ComfyUI, Ollamy, ACE-Step a správce souborů on-demand**. Existující Go gateway je prototyp, který chceme nahradit lehkým řešením ve sjednoceném Pythonovém projektu; nelze jej bez ověření nasazení jen smazat. Rezervační datový model existuje, plnohodnotné přidělování 0/1/N GPU a sdílená CPU kvóta jsou další fáze.

**Bezprostředně rozpracovaný krok:** migrace `011_allow_encryption_under_mount.sql` (asymetrické překryvy fscrypt rootů a mountů). V posledním doloženém výstupu z `ailab` byl soubor vytvořen, ale hlášen jako `??` (untracked); následné zařazení do Gitu, aplikování do DB a testy nejsou výstupem uživatele potvrzené. Na veřejném `main` k okamžiku kontroly migrace 011 nebyla (HTTP 404). **Nepovažovat ji za aplikovanou!**

## 2. Stav podle komponent

| Oblast | Stav | Co je doloženo / co chybí |
| --- | --- | --- |
| Struktura projektu | **V Gitu** | `compose.yml`, `control-plane/`, `db/migrations/`, `host/`, `gateway/`, `install/`, `templates/`. |
| PostgreSQL | **V Gitu; v minulosti testováno** | `postgres:18-bookworm`, databáze `ai_platform`, konfigurace přes Compose a secret soubor. Aktuální běh ověřit. |
| Control Plane | **V Gitu; dříve testováno** | FastAPI + administrace, identity přes Tailscale Serve, uživatelé, secrets, mounty a definice encryptable rootů. Aktuální běh ověřit. |
| Gateway API | **V Gitu; fscrypt API testováno** | Python FastAPI, `/health`, `/v1/me`, `/v1/storage`, fscrypt operace; není ještě úplnou obecnou gateway pro spouštění a proxy všech aplikací. |
| Host-agent | **V Gitu; v minulosti testováno** | Python služba na Unix socketu `/run/ai-platform/agent.sock`; verze `13` v kódu; mount operace, fscrypt operace a vytvoření Tailscale konektoru. Aktuální verzi na hostu ověřit. |
| Linux UID/GID a uživatelské adresáře | **Implementace + dřívější testy** | `host/ai-platform-helper.py`; adresáře v DATA a MODELS; pro testovacího uživatele byl provisioning ověřován. |
| Nativní FS quotas | **Schéma + helper** | `filesystem_quotas`; helper umí `setquota` a stav. Neprohlašovat za plně automatizované bez integračního testu. |
| Mounty | **Schéma + backend, testováno dílčím způsobem** | `user_mounts`, host-agent `apply/remove/status`, admin UI; lokální adresáře jsou přidělovány administrátorem. Dodat regresní testy pro překryvy, sítě a chybové stavy. |
| fscrypt | **Backend otestován na testovacím rootu** | Přes Gateway API ověřeny `status`, `enable`, `lock`, `unlock`, `change-passphrase`; po změně hesla staré odmítnuto, nové přijato. UI pro uživatele zatím není dokončeno. |
| `011_allow_encryption_under_mount.sql` | **Lokální rozpracovaná migrace** | Soubor prokazatelně existoval na serveru, ale nebyl naposledy prokazatelně commitnut ani spuštěn v DB. |
| Tailscale `ai-ts-<id>` | **Provisioning v kódu** | Trvalý kontejner `tailscale/tailscale`, persistentní state, `unless-stopped`, při provisioningu se auth key odstraní. Skutečný seznam a stav kontejnerů ověřit přes Docker. |
| Per-user webový rozcestník | **Návrh / prototyp** | `gateway/main.go`, `gateway/index.html`; chybí finální lehké uživatelské UI a jeho bezpečné napojení v konektoru. |
| Runtime aplikací (ComfyUI, Ollama, ACE-Step, file manager) | **Cíl, neprokázáno jako hotové v novém projektu** | On-demand start/stop, API proxy, 0/1/N GPU, CPU-only profily a vazba na vlastní storage jsou rozpracované zadání. |
| GPU scheduler | **DB návrh, runtime zbývá** | `gpu_devices`, `reservations.gpu_count`, `workloads`, `workload_gpus`; aktuální detekci GPU a kolizní plánování je třeba teprve ověřit/implementovat. |

## 3. Kód a jeho odpovědnosti

```text
compose.yml                           PostgreSQL, control-plane, gateway-api, Docker sítě
control-plane/app/main.py            administrátorské webové UI/API
control-plane/app/gateway_api.py     uživatelská identita konektoru, fscrypt API
control-plane/app/db.py             připojení k PostgreSQL
host/ai-platform-agent.py           privilegovaný Unix-socket agent (root)
host/ai-platform-helper.py          provisioning Linux adresářů a FS quotas
db/migrations/001_base.sql          základ: uživatelé, aplikace, GPU, storage, connectors
db/migrations/002_runtime.sql       reservations, workloads, workload_gpus
db/migrations/003...010...          provisioning, identity, secrets, mounty, fscrypt
gateway/main.go                      starý Go prototyp webové gateway
gateway/Dockerfile                   sestavení Go prototypu (není v Compose jako služba)
gateway/index.html                  minimální nefunkční HTML prototyp odemykání
install/configure-tailscale-serve.sh Tailscale Serve pro centrální control plane
```

`compose.yml` publikuje Control Plane pouze na `127.0.0.1:8080`. PostgreSQL není publikováno přímo. V konfiguraci jsou sítě `ai-control` (internal), `ai-frontend` a `ai-gateway` (internal). `gateway-api` běží jako samostatná Python služba a komunikuje s host-agentem přes připojený Unix socket. Produkční secrets jsou mimo repozitář v `/etc/ai-platform/secrets/`.

Důležité: skript `install/configure-tailscale-serve.sh` obsluhuje **hostitelský Control Plane**, nikoli automaticky rozcestník uvnitř každého `ai-ts-<id>`. Tyto dva scénáře nezaměňovat.

## 4. DB: migrace a význam

V kontrolované větvi `main` jsou přítomné migrace `001_base.sql` až `010_user_encryption_roots.sql`, včetně:

- `001_base`: `users`, `hosts`, `applications`, `application_profiles`, `gpu_devices`, `storage_resources`, `filesystem_quotas`, `user_connectors` a další základní entity;
- `002_runtime`: `reservations` s `gpu_count`, `workloads` s režimy `preparation`/`gpu`, `workload_gpus`;
- `003`–`007`: provisioning, pravidla username, šifrované platform secrets, admin a identita Tailscale connectoru;
- `008_user_mounts`, `009_user_mounts_extensions_only`: přidělené mounty a pravidla kořenových cest;
- `010_user_encryption_roots`: správa encryptable rootů a současné přísné překryvové triggery.

**Záměr `011`:** povolit definici fscrypt rootu *pod již existujícím* uživatelským mountem, ne na shodné cestě nebo opačným směrem. Opačný trigger zakazující dodatečné překrývání mounty se ponechává. Při `enable` je potřeba ověřit skutečnou podporu fscrypt na výsledném backing filesystemu. Základní `/AI/models` se fscrypt nezapíná, ale vhodný připojený filesystem uvnitř MODELS může šifrování podporovat.

Před migrací porovnat serverový soubor, repository a DB `schema_migrations`; **nepřepisovat aplikovanou migraci 010**.

## 5. Co už bylo skutečně otestováno

Z terminálové historie 3. listopadu 2026:

- `ai-platform-agent` odpovídal ve verzi `13` (po změnách kolem změny hesla).
- Gateway API obsloužilo zkušební fscrypt root uživatele `test` pod `/AI/data/users/test/private2`.
- Ověřen byl úspěšný `lock` a `unlock`.
- Po `change-passphrase` se **staré heslo odmítlo**, zatímco **nové fungovalo**.
- Změny Gateway API pro změnu hesla byly commitnuty a pushnuty (`ca505d7` podle posledního doloženého push výstupu). Neznamená to automaticky, že serverový pracovní strom je nyní čistý.
- `/AI/models` byl zjištěn jako filesystem bez setupu fscrypt, což bylo **záměrně ponecháno** kvůli sdílení/deduplikaci modelů.

Testovací hesla, skutečné secret values, tokeny a osobní Tailscale identifikátory **do dokumentace ani Git historie neukládat**.

## 6. Právě dohodnuté chování — ještě k implementaci

1. **Konektor:** trvale běží jeden lehký `ai-ts-<id>` na uživatele, nezávisle na GPU rezervaci. Bez Docker socketu.
2. **Uživatelské UI:** lehký rozcestník vždy dostupný; správa šifrování a rezervační kalendář mohou používat centrální API bez dalšího trvale běžícího aplikačního kontejneru.
3. **Ostatní služby:** file manager, ComfyUI, Ollama/Open WebUI, ACE-Step se spouštějí **ručně on-demand**. Nezůstávají trvale běžet, pokud je uživatel nepotřebuje.
4. **Ollama API:** dostupné na stabilní adrese **až po ručním spuštění**. API volání nesmí Ollamu samo spustit. Zachovat nativní i OpenAI-kompatibilní endpointy a streamování.
5. **Bez rezervace:** aplikační kontejner **nemá GPU**, náročné AI aplikace sdílejí **jednu společnou** nízkou CPU kvótu (počáteční návrh 1 logické CPU celkem). Správce souborů, rozcestník a řídicí služby mají samostatné prostředky.
6. **S rezervací:** kontejner získá 1 konkrétní GPU, případně 2 GPU současně pro jediného uživatele/aplikaci, nebo obecně N. Žádná aplikace nemůže GPU obejít.
7. **Jeden kontejner:** pro běžící instanci služby nepoužívat oddělený Comfy editor a GPU worker; podle potřeby se před přidělením/odebráním GPU instance bezpečně znovu vytvoří.
8. **Storage:** mounty připravuje host-agent; admin spravuje lokální zdroje, uživatel může pracovat jen se svými oprávněními a případnými povolenými síťovými zdroji.
9. **Technologie:** vlastní serverový projekt sjednotit na Pythonu. Současný Go web nahradit, ale neodstraňovat bez ověření skutečného nasazení.

Podrobná architektura: [ARCHITECTURE.md](ARCHITECTURE.md).

## 7. Otevřené úkoly v doporučeném pořadí

| Pořadí | Úkol | Stav dokončení |
| --- | --- | --- |
| **1** | Ověřit `git status`, obsah a přítomnost `011_allow_encryption_under_mount.sql` na serveru a DB `schema_migrations`. | neověřeno |
| **2** | Projít obsah `011`, bezpečně ji aplikovat do PostgreSQL a otestovat oba směry overlap pravidla + případ fscrypt pod vhodným mountem. | neověřeno / nedokončeno |
| **3** | Po testech commitnout a pushnout `011`, aktualizovat tento dokument. | nehotovo |
| **4** | Inventura běžících `ai-ts-<id>`, jejich Tailscale Serve konfigurace a případného nyní běžícího Go webu, **bez restartu**. | neověřeno |
| **5** | Navrhnout minimální rozcestník a bezpečnou per-user autentizaci vůči `gateway-api`; ověřit dostupnost přes Tailscale, bez výchozího Go stacku. | plán |
| **6** | Zprovoznit ovládání šifrování v uživatelském rozcestníku a ruční on-demand start/stop aplikace. | plán |
| **7** | Stabilní Ollama API přes konektor: po ručním startu funguje, při vypnutí se nic samo nespouští; otestovat streaming. | plán |
| **8** | Sdílený nízký CPU fond, 0/1/N GPU, atomické rezervace a per-user kontejnerový runtime. | plán |
| **9** | Regresní testy mountů, fscrypt, oprávnění, kvót a cleanup při chybách. | plán |
| **10** | Volitelný VeraCrypt vault a jeho přenos/synchronizace. | později, jen návrh |

### Bezpečnostní oblasti před nasazením dalším uživatelům

- Ujistit se, že `Tailscale-User-*` hlavičky nemůže podvrhnout klient přistupující k Control Plane mimo důvěryhodný Tailscale Serve.
- `gateway_api.py` nyní identifikuje konektor podle `X-AI-Connector-ID`. Je nutné vyřešit důvěryhodnou cestu/ověření volajícího, aby interní kontejner nemohl impersonovat cizí identitu.
- U aplikací v uživatelském namespace zabránit přístupu k PostgreSQL, Docker socketu, host-agent socketu a datům ostatních uživatelů.
- U root operací řešit chybové stavy, kdy se změna na hostu provede, ale DB transakce nepotvrdí.
- Jednorázově prověřit veřejnou historii Git repozitáře na omylem commitnuté secrets; Tailscale sám takový únik neřeší.

## 8. Bezpečné první ověřovací příkazy po navázání

Příkazy níže **nic nemění**. Výstupy předává správce hostitele; nikdy neposílat skutečné secret values.

```bash
cd /AI/apps/ai-platform

git status --short
git log -1 --oneline
ls -l db/migrations/011_allow_encryption_under_mount.sql

docker compose --env-file /etc/ai-platform/compose.env -f compose.yml ps

docker ps -a --filter 'name=ai-ts-' \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

Databázové ověření verze migrací až po kontrole stavu Compose:

```bash
docker compose --env-file /etc/ai-platform/compose.env -f compose.yml \
    exec -T postgres \
    psql -U ai_platform -d ai_platform \
    -c 'SELECT version, applied_at FROM schema_migrations ORDER BY version;'
```

Následná úprava DB nebo restart služeb **až po vyhodnocení výstupů**, ne naslepo.

## 9. Pravidla vedení dokumentace a předávání konverzací

- Po dokončení většího celku aktualizovat **[PROJECT_STATE.md](PROJECT_STATE.md)**: co je implementováno, co bylo otestováno, jakým commitem a co zbývá.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** aktualizovat jen při změně rozhodnutých pravidel nebo zásadního technického návrhu.
- Při práci vždy rozlišovat stav `main`, necommitnuté změny na `ailab` a stav běžících služeb.
- Do další konverzace stačí odkázat na repo, tyto dva dokumenty a dodat poslední `git status`/důležité runtime výpisy. Historii chatu není nutné znovu kopírovat.
- Tyto dokumenty **nenahrazují aktuální výpis serveru** a neověřují se samy; aktualizovat je průběžně a při blížícím se limitu konverzace připravit přesný checkpoint.
