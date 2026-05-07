# NAS Downloader — Inštalácia od nuly

## Čo budeš potrebovať
- Synology NAS s DSM 7.x
- Prístup do DSM cez prehliadač
- Vytvorené priečinky pre filmy a seriály (napr. `/volume1/films` a `/volume1/movies`)

---

## Krok 1 — Nainštaluj Container Manager (Docker)

1. Otvor **DSM → Package Center**
2. Vyhľadaj `Container Manager`
3. Klikni **Inštalovať**

---

## Krok 2 — Zapni SSH

1. **DSM → Control Panel → Terminal & SNMP**
2. Zaškrtni **Enable SSH service**
3. Klikni **Apply**

---

## Krok 3 — Pripoj sa cez SSH

Na PC otvor terminál (Windows: PowerShell alebo PuTTY):

```bash
ssh tvoje-meno@192.168.1.XXX
```

IP adresu nájdeš v DSM → vpravo hore → meno účtu → IP.

---

## Krok 4 — Vytvor priečinok pre projekt

```bash
mkdir -p /volume1/docker/nas-downloader
cd /volume1/docker/nas-downloader
```

---

## Krok 5 — Stiahni súbory projektu

```bash
wget -O nas-downloader.zip https://github.com/AsTerqq/nas-downloader/archive/refs/heads/main.zip
unzip nas-downloader.zip
mv nas-downloader-main/* .
rm -rf nas-downloader-main nas-downloader.zip
```

---

## Krok 6 — Nastav svoje cesty

Skopíruj vzorový config:

```bash
cp .env.example .env
nano .env
```

Uprav podľa seba:

```
MOVIES_PATH=/volume1/films
SERIES_PATH=/volume1/movies
```

Ulož: `Ctrl+X` → `Y` → `Enter`

---

## Krok 7 — Spusti

```bash
sudo docker compose up -d --build
```

Počkaj ~2 minúty (prvé spustenie sťahuje modely).

Otvor v prehliadači: `http://192.168.1.XXX:8080`

---

## Aktualizácia (keď vyjde nová verzia)

```bash
cd /volume1/docker/nas-downloader
wget -O update.zip https://github.com/AsTerqq/nas-downloader/archive/refs/heads/main.zip
unzip -o update.zip
mv nas-downloader-main/* .
rm -rf nas-downloader-main update.zip
sudo docker compose up -d --build
```

---

## Problémy?

- **Stránka sa neotvorí** → skontroluj či beží: `sudo docker ps`
- **Chyba pri spustení** → pozri logy: `sudo docker compose logs -f`
- **Port 8080 obsadený** → v `docker-compose.yml` zmeň `"8080:8080"` na napr. `"8081:8080"`
