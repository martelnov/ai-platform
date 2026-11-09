# AI Platform — architektura a dohodnuté principy

**Stav návrhu:** 9. listopadu 2026
**Projekt:** `martelnov/ai-platform`  
**Aktivní vývojová větev:** `feature/user-provisioning-fscrypt` (`ac3b99e`)
**Související projekt:** `martelnov/SalamanderRS`, větev `web-v0.10` (`fa51575`)
**Účel dokumentu:** zachytit cílová pravidla i již rozhodnutou technickou topologii. **Cílový návrh není automaticky implementovaný.** Ověřené funkce a otevřené úkoly jsou v [PROJECT_STATE.md](PROJECT_STATE.md).

## 1. Cíl

Vytvořit přenosnou víceuživatelskou platformu pro sdílení jedné nebo více GPU. Uživatel má vlastní trvalou síťovou identitu a úložiště; aplikace (ComfyUI, Ollama/Open WebUI, ACE-Step a další) se spouštějí pouze podle potřeby. Rezervace GPU se odděluje od přístupu do uživatelského prostředí.

Platformu provozuje **jeden správce**, který má práva `root` na hostiteli a je administrátorem aplikace. Ostatní uživatelé nedostávají administrátorská oprávnění hostitele.

Hlavní zásady:

1. **Tailscale je vstupní síťová vrstva.** Služby nevystavovat do veřejného internetu.
2. **Jeden persistentní `ai-ts-<id>` na uživatele.** Jeho Tailscale identita a DNS zůstávají stabilní i mezi rezervacemi.
3. **Rozcestník je dostupný stále; pracovní aplikace jsou on-demand.** Rozcestník tvoří statické HTML/JS + malý Python launcher. API požadavek aplikaci automaticky nespustí.
4. **O GPU rozhoduje centrální plánovač, ne aplikace ani uživatelský kontejner.** Bez rezervace aplikace GPU vůbec nedostane.
5. **Data a konfigurace jsou mimo dočasné aplikační kontejnery.** Restart/recreate aplikace nesmí smazat uživatelskou práci.
6. **Privilegované operace provádí host-agent na hostiteli.** Uživatelské služby nemají Docker socket ani root přístup na hostitele.
7. **Projekt má být přenositelný na jiný server.** Hardware, limity a cesty nesmějí být napevno zakódovány do uživatelské logiky.

## 2. Topologie: ověřený SalamanderRS a cílové rozšíření

```text
Správce přes Tailscale
        |
        +--> Tailscale Serve hostitele --> Control Plane (Python/FastAPI)
                                                |        |
                                                |        +--> PostgreSQL
                                                +--> Unix socket --> Host-agent (root)
                                                                      |
Uživatel: sdílený Tailscale stroj                                   správa Dockeru,
        |                                                             mountů, fscrypt
        v
  ai-ts-<id> [TRVALE, HTTPS, stabilní identita]
        |
        +--> HTTPS :443  --> 127.0.0.1:8080 --> ai-launcher-<id> [TRVALE]
        |                                         - statický HTML/JS rozcestník
        |                                         - malý Python HTTP/proxy
        |                                         - HMAC identita konektoru
        |                                                 |
        |                                         Gateway API [centrální]
        |                                          |               |
        |                                        PostgreSQL    Host-agent
        |
        +--> HTTPS :8443 --> 127.0.0.1:8090 --> ai-files-<id> [ON DEMAND]
                                                  SalamanderRS, bez GPU
                                                  /data a /models per-user

  DALŠÍ PLÁNOVANÉ ON-DEMAND SLUŽBY (dosud neintegrované):
    ComfyUI                  0/1/N GPU
    Ollama / Open WebUI      0/1/N GPU; nativní i kompatibilní API
    ACE-Step                 podle režimu
    další služby             podle profilu
```

**Ověřená část:** `ai-ts-<id>`, launcher, HMAC Gateway API, Tailscale Serve na portech 443 a 8443 a utility workload SalamanderRS. Pro Salamander se používá Docker síťový namespace `--network container:ai-ts-<id>`; web funguje bez publikace hostitelských portů. Správce přes Tailscale má oddělený přístup k centrálnímu Control Plane.

**Cílová část:** obdobné on-demand připojení dalších aplikací a kompletní orchestrace 0/1/N GPU. Zda všechny budoucí aplikace mohou sdílet tutéž topologii a jak budou vyřešeny jejich porty, streaming a WebSockety, se musí teprve ověřit.

Tailscale konektor ani launcher **nesmějí přímo ovládat Docker**. Launcher přijímá uživatelské akce, autentizuje volání do Gateway a host-agent provádí pouze serverově autorizované operace.

## 3. Uživatelský konektor, launcher, identita a rozcestník

Při provisioningu dostane uživatel Linux UID/GID, vlastní stromy DATA/MODELS a trvalý Tailscale uzel `ai-ts-<id>`. Konektor běží nezávisle na GPU rezervaci; sdílení zařízení pomocí Tailscale machine sharing je samostatný krok a plnou automatizaci pozvání ještě nemáme doloženou.

**Rozcestník je technicky rozhodnut:** `gateway/index.html` (statické HTML/JavaScript) a `gateway/launcher.py` (malý Python HTTP server). Žádný nový Go aplikační server není potřeba; `gateway/main.go` zůstává historickým prototypem. Launcher běží jako `ai-launcher-<id>` a zprostředkuje pouze předem povolené API cesty.

**Bezpečné napojení:** hostitelský launcher manager odvodí pro každý `provider_id` samostatný token pomocí HMAC-SHA256 z root klíče `/etc/ai-platform/secrets/launcher_auth.key`. Root klíč zůstává pouze na důvěryhodných částech platformy; launcher dostane odvozený token. Gateway vyžaduje `X-AI-Connector-ID` **společně** s `X-AI-Connector-Auth`, porovná HMAC v konstantním čase a z databáze dohledá povolený konektor a uživatele. Samotná hlavička s ID už není důkazem identity. Provozní revize dalších přístupových cest a izolace stále zbývá.

**Tailscale Serve uživatele:** port 443 směruje na launcher (`127.0.0.1:8080`), port 8443 na SalamanderRS (`127.0.0.1:8090`). Manager má konfiguraci 8443 přidávat pouze při shodě očekávaných pravidel; nesmí přepsat jinou existující konfiguraci. U vypnutého SalamanderRS zůstává druhý Serve route definovaný, ale backend není dostupný a žádný požadavek jej automaticky nespustí.

Rozcestník má nabízet:

| Funkce | GPU | Stav / provoz |
| --- | --- | --- |
| Stav a ovládání SalamanderRS | ne | **Funguje:** Spustit, Otevřít, Ukončit |
| Souborový přístup DATA/MODELS | ne | **Funguje v SalamanderRS:** pouze přidělené prostory |
| Správa šifrování | ne | Backend dříve testován; dokončení / regrese UI zbývá |
| Rezervační kalendář | ne | Plánované lehké UI přes centrální API |
| Správa modelů / LoRA | ne | Návrh a integrace zbývají |
| ComfyUI | 0/1/N | On-demand po implementaci GPU runtime |
| Ollama / Open WebUI | 0/1/N | On-demand; API pouze během běhu |
| ACE-Step | dle režimu | On-demand po integraci |

Tento výběr technologie rozcestníku a SalamanderRS již není otevřenou architektonickou otázkou.

## 4. Životní cyklus workloadů a jejich API

### Ověřený vzor: SalamanderRS

1. Uživatel se připojí přes Tailscale k vlastnímu rozcestníku.
2. Pro požadovaný šifrovaný prostor provede případně unlock (backend fscrypt byl testován dříve).
3. Uživatel zvolí **Spustit**. Launcher předá požadavek centrální Gateway s HMAC autentizací.
4. Gateway odvodí identitu konektoru a příslušný **utility workload** z PostgreSQL. Prohlížeč neposílá libovolné `user_id`, `connector_id`, `workload_id`, Docker parametry ani cestu k image.
5. Host-agent validuje konfiguraci, UID/GID, image, mounty a aktuální Tailscale kontejner. Spustí izolovaný `ai-files-<id>` ve sdíleném network namespace. Viditelná úložiště: `/data` a `/models`, mapovaná z vlastních uživatelských stromů.
6. **Otevřít** použije HTTPS `:8443`, nikoli veřejný port hostitele.
7. **Ukončit** vede přes Gateway k agentovi, který provede Docker stop. SalamanderRS od commitu `fa51575` podporuje graceful shutdown po SIGTERM; otestované ukončení bez SIGKILL proběhlo přibližně za 4 sekundy. Chování během aktivního dlouhého přenosu je otevřený test.

Data zůstávají mimo dočasný aplikační kontejner. Výchozí Docker stop timeout u agenta je 20 sekund jako bezpečnostní pojistka; není to běžná naměřená doba ukončení. Při dlouhém zápisu je nutné výslovně testovat konzistenci a obnovení přenosu.

### Cílový vzor: ComfyUI / Ollama / ACE-Step

1. Uživatel ručně spustí službu a platforma ověří storage, rezervaci a dostupné zdroje.
2. Kontejner získá CPU-only profil bez GPU nebo explicitně přidělených 1 až N GPU.
3. Během běhu zůstává dostupné webové UI i nativní API; u Ollamy `/api/*` a kompatibilní `/v1/*`. Proxy musí zachovat streamování, pro ComfyUI WebSockety.
4. Po ručním stop je backend nedostupný. **API požadavek jej nesmí automaticky probudit.**

Na instanci počítáme s jedním aplikačním kontejnerem. Přechod CPU ↔ GPU pravděpodobně vyžaduje jeho recreate; persistentní data se zachovají, ale neuložený stav otevřeného UI zatím není garantován. Automatický idle shutdown zůstává jen možnou budoucí funkcí; nesmí přerušit aktivní výpočet nebo přenos.

Stabilní URL/portová mapa *dalších* aplikací ještě není definitivně stanovena.

## 5. GPU a CPU plánování

### GPU

- Rezervace požaduje **počet GPU**, nikoli napevno aplikaci: po získání rezervace může uživatel přecházet mezi různými podporovanými aplikacemi.
- 0 GPU = přípravný režim, žádný přístup k GPU zařízením.
- 1 GPU = výhradně přidělená konkrétní karta.
- 2 GPU = obě karty mohou být přiděleny **jednomu aplikačnímu kontejneru jednoho uživatele** (např. Ollama, některé workflow ComfyUI).
- Obecně **N GPU**: datový model má zůstat rozšiřitelný. Přidělení více karet musí být atomické (všechny požadované, nebo žádná).
- Dvě RTX 3060 12 GB **neznamenají jednu univerzální 24GB VRAM**. Využití obou závisí na aplikaci/modelu.
- Jednotlivé GPU identifikovat stabilně pomocí GPU UUID, nikoli jen pořadového čísla v Dockeru.

### CPU bez rezervace

Všechny **výpočetní AI kontejnery bez GPU rezervace** mají dohromady sdílet velmi malý CPU fond, původně uvažovaný ekvivalent **jednoho logického CPU celkem**. **Ne jedno CPU na uživatele ani na kontejner.** Velikost fondu nastaví administrátor podle finálního hardware. Lze realizovat společnou nadřazenou cgroup s agregovanou CPU kvótou; konkrétní způsob je nutné implementovat a změřit.

Služby potřebné pro ovládání platformy a souborový přístup nesmějí být tímto fondem zablokovány. Pro rezervovanou GPU relaci se nastaví vyšší CPU/RAM limity podle profilu služby. Limity nejsou napevno dané procesorem v první sestavě.

### Datový model: co již existuje

Migrace `001_base.sql` obsahuje `gpu_devices` (včetně `gpu_uuid`) a `application_profiles` (`gpu_min`, `gpu_max`, `cpu_limit`, `memory_limit_bytes`). Migrace `002_runtime.sql` obsahuje `reservations.gpu_count`, `workloads` s režimy `preparation` a `gpu` a vazební tabulku `workload_gpus`. **Existence tabulek ještě neznamená hotový a otestovaný plánovač.** Při implementaci je nutné teprve prověřit, zda je třeba doplnit vazbu konkrétní rezervace na konkrétní GPU; nevytvářet druhou duplicitní tabulku bez revize stávajícího návrhu.

## 6. Úložiště, uživatelé, kvóty a šifrování

### Uživatelské prostory

- Základní stromy jsou **DATA** a **MODELS** (`/AI/data/users/<username>`, `/AI/models/users/<username>`).
- Každý uživatel má vlastní Linux UID/GID a vlastní přístupová práva; podrobnosti přiřazení řídí hostitelská platforma.
- Modely mohou být ve společném nezakódovaném/nešifrovaném prostoru pro sdílení a deduplikaci; soukromé uživatelské modely mohou být podle přiděleného backing filesystemu šifrované.
- Diskové kvóty jsou **nativní filesystem quotas**; hodnoty a požadovaný stav jsou v PostgreSQL. Neimplementovat paralelní softwarovou simulaci kvót.

### Mounty

- Administrátor smí uživateli přidělit i adresář / blokové zařízení z jiného místa hostitele (případně sítě), protože sám spravuje celý server.
- Běžný uživatel nesmí libovolně mountovat lokální systémové cesty. Možnost připojovat jeho vlastní síťové zdroje (SMB/NFS) je samostatné plánované oprávnění s validací a limity.
- Uživatel vidí přidělené mounty. Vlastní mount/unmount operace provádí privilegovaný host-agent, **ne** dlouhodobě běžící Tailscale konektor ani krátkodobý privilegovaný storage kontejner.
- Mounty a přístup k jejich datům musejí být připraveny **před** spuštěním cílové aplikace. Zastavení aplikace nesmí samo smazat uložená data.

### fscrypt

- **Administrátor definuje, které konkrétní podadresáře smějí být šifrovatelné; uživatel si je sám inicializuje, odemyká, zamyká a mění heslo.**
- fscrypt je vlastnost **reálného backing filesystemu**, ne logického názvu stromu DATA/MODELS.
- Na základním `/AI/models` se fscrypt nepovoluje kvůli plánovanému sdílení/deduplikaci modelů; přesto **nesmíme zakázat fscrypt v celé větvi MODELS**. Šifrovaný root může ležet uvnitř připojeného jiného filesystemu, který fscrypt skutečně podporuje a je připravený.
- `010_user_encryption_roots.sql` původně blokovala překryvy mountů oběma směry. `011_allow_encryption_under_mount.sql` byla navržena tak, aby povolila **encryption root uvnitř již existujícího mountu** (např. mount `/private`, encryption `/private/secret`), ale dál odmítala stejnou cestu, mount uvnitř encryption rootu a dodatečný překrývající mount. **Skutečný stav migrace 011 v DB k 9. 11. ověřit**, neoznačovat automaticky za aplikovanou. Migrace `010` se zpětně nepřepisuje.
- Před inicializací šifrování musí host-agent ověřit skutečný filesystem a jeho podporu/setup fscrypt. **To je nutné ještě prakticky otestovat i na podadresáři připojeného filesystemu.**
- Soubory odemčené na serveru jsou dostupné hostitelskému `root`. Toto řešení **nechrání plaintext před administrátorem v době, kdy je úložiště odemčené**.

### VeraCrypt — zatím pouze úvaha

Vedle fscrypt se diskutovala podpora uživatelem vytvořeného přenositelného `.vc` vaultu: soubor se na server nahraje (nebo přenese fyzicky), uživatel jej odemkne, použije a zase zamkne a může si jej stáhnout. Možná je též pozdější synchronizace rozdílů mezi identickými kopiemi. **VeraCrypt zatím není součástí hotové implementace ani aktuální priority.**

## 7. Bezpečnostní hranice

- `root` hostitele a administrace platformy patří jediné důvěryhodné osobě.
- Tailscale zajišťuje síťový dosah, ale **nenahrazuje aplikační autorizaci**. Uživatel smí ovládat pouze své workloady a vidět jen přidělená data.
- Pro ověřenou per-user cestu byl implementován **HMAC-SHA256 token odvozený pro konkrétní konektor**. Gateway validuje hlavičku `X-AI-Connector-Auth` proti `X-AI-Connector-ID` a pak ověřuje identitu v DB. Samotný `X-AI-Connector-ID` nesmí stačit.
- Root klíč launcherů je secret mimo Git a mimo běžné uživatelské kontejnery. Ani odvozené tokeny nesmějí proniknout do HTML, logů nebo veřejné historie repozitáře.
- `Tailscale-User-*` hlavičky pro centrální administraci smějí být důvěryhodné pouze při vynuceném průchodu přes správnou Tailscale Serve proxy.
- Uživatelská služba nemá Docker socket, PostgreSQL credentials ani host-agent socket; nesmí dostat systémové rooty nebo adresáře jiného uživatele.
- Host-agent musí validovat skutečný cílový Docker objekt a serverový stav, nejen ID z klientského požadavku. Změny DB ↔ Docker musejí mít bezpečné chování při částečném selhání.
- Existující HMAC a testy **neznamenají dokončený audit** proti všem cestám, únikům tokenů, SSRF, chybám symlink/mount izolace a souběžným operacím.
- Veřejné GitHub repozitáře jsou určené pro zdrojový kód, nikoli pro hesla, API klíče a jiné secrets; samotná změna viditelnosti repo historie nic nemaže.

## 8. Hardware a přenositelnost

Uvažovaná sestava pro GPU server je **Dell Precision T3610**, jeden **Xeon E5-2697 v2** (12 jader / 24 vláken), nejvýše **2× NVIDIA RTX 3060 12 GB**. Druhá karta by případně měla vlastní externí ATX zdroj. Jde o **plánovanou možnost**, nikoli doklad, že jsou obě GPU již osazené nebo že na ní nyní běží `ai-platform`.

Současné vývojové/testovací výpisy pocházejí ze serveru `ailab` s Ubuntu 24.04.5 LTS. Hardwarové parametry a skutečně instalované GPU je nutné vždy ověřit přímo na hostiteli. Software musí fungovat i na budoucím stroji s jiným počtem GPU.

## 9. Co ještě není rozhodnuto nebo dokončeno

- Definitivní URL/portová mapa dalších on-demand aplikací, jejich streaming, WebSockety a případné proxy limity.
- GPU scheduler a cgroup pravidla pro 0/1/N GPU, sdílenou nízkou CPU kvótu a přechod CPU ↔ GPU.
- Pravidla pro bezpečný stop při dlouhém obnovitelném přenosu, když klient právě zapisuje.
- Reprodukovatelné sestavení image SalamanderRS z Git commitu a mapování aktuálního image tagu na DB. Na testovacím serveru proběhla jednorázová ruční výměna tagu `0.10.0-7804126` za `0.10.0-sigterm`; čistá instalace to zatím neřeší.
- Ověření stavu migrací 011/012/013 proti databázi, případně nová migrace pro image; staré aplikované migrace neměnit.
- Dokončení rezervačního kalendáře, integrace správy modelů/LoRA a dalších AI aplikací.
- **Automatické nasazení a aktualizace z GitHubu** je cílový požadavek na přenositelnost, ale instalátor se bude řešit až později.

**Již rozhodnuto:** technologie rozcestníku (statické HTML/JS + Python launcher), HMAC autentizace konektorů a SalamanderRS jako první správce souborů on-demand.

## 10. Pravidlo pro další vývoj

Před každou větší změnou porovnat **kód aktuální vývojové větve na GitHubu** s **aktuálním stavem běžícího serveru**. Nikdy nepokládat plánované chování za již hotové jen proto, že existuje databázová tabulka nebo ukázkové HTML. Po ověření funkce doplnit [PROJECT_STATE.md](PROJECT_STATE.md) a zdokumentovat rozhodnutí, které změnilo architekturu.
