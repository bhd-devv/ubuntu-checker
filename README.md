# Beszel + Prometheus для Ubuntu та Coolify

Beszel — основний вебінтерфейс для CPU, RAM, дисків, мережі та Docker-контейнерів.
Prometheus паралельно збирає метрики Node Exporter і cAdvisor у фоні, незалежно від Beszel.
Обидві системи зберігають історію за останні **30 днів** у постійних Docker volumes.

```text
Браузер → HTTPS / Coolify Proxy → Beszel Hub:8090
                                    └─ Unix socket → Beszel Agent → хост і Docker

Prometheus:9090 (внутрішній) ─┬─ docker0 IP:9100 → Node Exporter → хост
                            └─ cadvisor:8080  → cAdvisor → Docker-контейнери
```

Beszel має власну базу й не використовує Prometheus як datasource. Їхні значення можуть трохи відрізнятися через різні формули та інтервали збору.
Стек моніторить один Linux-сервер, на якому його задеплоєно, включно з іншими застосунками Coolify.

## Історія за місяць

| Система | Зберігання | Деталізація |
| --- | --- | --- |
| Prometheus | `--storage.tsdb.retention.time=30d`, без обмеження розміром | Усі зібрані точки кожні 30 секунд протягом 30 днів |
| Beszel 0.21.0 | Вбудовані 30 днів у базі Hub | Старі дані автоматично усереднюються згідно з таблицею нижче |

Політика Beszel зафіксована у його коді; змінної `RETENTION_DAYS` у цій версії немає.
Вона застосовується до статистики сервера й контейнерів:

| Тип запису Beszel | Зберігається |
| --- | --- |
| 1 хвилина | 1 година |
| 10 хвилин | 12 годин |
| 20 хвилин | 24 години |
| 2 години | 7 днів |
| 8 годин | 30 днів |

Отже, місячний графік Beszel показує тренд з усередненням за 8 годин. Для аналізу коротких піків за будь-який день місяця використовуй Prometheus.
Це історія метрик, не архів усіх логів Docker. Журнал сповіщень Beszel має окрему політику за кількістю записів.

Prometheus видаляє старі TSDB blocks асинхронно, тому фізична межа очищення може трохи перевищувати 30 днів.
Попередній ліміт 5 GB прибрано: він міг видаляти дані раніше місяця.
Потрібне вільне місце для всіх 30 днів, WAL/head і бази Beszel. Стеж за диском у Beszel; заповнення диска може зупинити запис метрик.
Історія накопичується з моменту запуску; зміна retention не відновлює вже видалені дані.

## 1. Перевірка сервера

Потрібен звичайний **rootful Docker Engine на Ubuntu/Linux**. Docker Desktop показує Linux VM; rootless Docker і обмежені LXC потребують іншого доступу до хоста.

```bash
ip -4 -o addr show docker0
docker info --format '{{.DockerRootDir}}'
test -c /dev/kmsg && echo '/dev/kmsg OK'
test -d /dev/disk && echo '/dev/disk OK'
```

`NODE_EXPORTER_HOST` — IPv4 `docker0`, типово `172.17.0.1`. Адреса має існувати на хості та бути доступна з Docker-мережі стека.
Node Exporter використовує host networking, але слухає лише цю внутрішню адресу на 9100. Не вказуй публічну IP або `0.0.0.0`.
Якщо firewall блокує доступ, дозволь його з підмережі цього Compose-проєкту до `docker0:9100`.

Якщо DockerRootDir відрізняється від `/var/lib/docker`, встанови фактичний шлях у `DOCKER_DATA_ROOT`.
Beszel Agent читає кореневу файлову систему через `/host` (`FILESYSTEM=/host`) та використовує host networking для мережевих лічильників.
Для окремих дисків за потреби додай агенту read-only bind mounts у `/extra-filesystems/<назва>`; Node Exporter уже бачить файлові системи через host root із `rslave`.

Beszel Agent отримує Docker socket; cAdvisor використовує `privileged`, host cgroups та системні mounts.
Це довірені агенти з доступом до хоста. Read-only монтування socket не обмежує Docker API лише читанням.
Публічний домен призначай лише Beszel Hub.

## 2. Перший деплой у Coolify

Початкове налаштування має два кроки: спочатку Hub створює власний SSH-ключ, потім Agent отримує його **публічну** частину.
Prometheus та експортери працюють уже на першому етапі.
Окремий `compose.setup.yml` дозволяє виконати перший крок без непрацездатного агента і без послаблення перевірок готовності.

1. Завантаж усі файли проєкту в Git-репозиторій. `.env` із секретами не коміть.
2. У Coolify створи **Git Repository application** на потрібному сервері з build pack / strategy **Docker Compose**.
3. Задай **Base Directory** `/`, початковий **Docker Compose Location** `/compose.setup.yml`.
4. Увімкни **Preserve repository during deployment / Preserve Repository**: фінальний стек монтує конфігурації Prometheus із Git.
5. У **Environment Variables** встанови:

   | Змінна | Значення |
   | --- | --- |
   | `BESZEL_APP_URL` | `https://monitor.example.com` — реальний URL без внутрішнього `:8090` |
   | `BESZEL_USER_EMAIL` | Email початкового адміністратора |
   | `BESZEL_USER_PASSWORD` | Власний довгий випадковий пароль, без зовнішніх лапок |
   | `NODE_EXPORTER_HOST` | IPv4 інтерфейсу `docker0` |
   | `DOCKER_DATA_ROOT` | Зміни лише якщо DockerRootDir відрізняється від `/var/lib/docker` |

6. Спрямуй DNS на сервер Coolify proxy. У **Domains** вибери сервіс **beszel** і задай `https://monitor.example.com:8090`.
   `:8090` тут задає порт контейнера; браузер відкриває звичайний `https://monitor.example.com`.
7. Натисни **Deploy**, відкрий Beszel і увійди з email та паролем із env.
8. Натисни **Add System**, задай назву сервера та **Host / IP** `/beszel_socket/beszel.sock`. Порт можна залишити стандартним `45876`: для Unix socket він не використовується.
9. Скопіюй публічний ключ із цієї форми (рядок `ssh-ed25519 ...`), збережи систему та додай у Coolify `BESZEL_AGENT_KEY` із цим ключем. Не підставляй token або приватний ключ.
10. Перевір збережені `NODE_EXPORTER_HOST`, `DOCKER_DATA_ROOT` та, за потреби, `DOCKER_ENDPOINT` (див. `.env.example`).
11. У **тій самій** Coolify application зміни **Docker Compose Location** на `/docker-compose.yml`, збережи та перезавантаж конфігурацію з Git.
    Переконайся, що домен лишився привʼязаним до сервісу **beszel:8090**.
12. Натисни **Deploy**. Залиш звичайний режим Compose, **Raw mode не вмикай**. Не додавай домени для агента, Prometheus чи експортерів.

Назви сервісу `beszel` та volumes збігаються в обох Compose-файлах, тому дані Hub зберігаються при перемиканні в межах тієї самої Coolify application.
Не створюй другу application між кроками: вона отримає інші volumes і новий ключ.
Для цього підключення `TOKEN` і `HUB_URL` агенту не потрібні: Hub сам опитує його через спільний Unix socket із SSH-автентифікацією.

Після підключення агент стане healthy, а система в Beszel — Up. На перший цикл збору дай 1–2 хвилини.
Prometheus працює незалежно; доступність Beszel не блокує його старт.

## Якщо попередній стек із Grafana вже був задеплоєний

Оновлюй **той самий Git-репозиторій і ту саму Coolify application**.
Для двокрокового переходу спочатку використай `/compose.setup.yml`: він містить також Prometheus із експортерами, тому фоновий моніторинг працює на обох етапах.
Його `prometheus-data` volume використовується в обох файлах. Звичайний redeploy може створити коротку паузу збору під час перезапуску сервісів.
Раніше зібрана історія залишиться, якщо volume не видаляти.

Після переходу перевір домен: сервіс тепер `beszel`, внутрішній порт **8090** замість Grafana 3000.
Старі `GRAFANA_*`, `PROMETHEUS_RETENTION_TIME` і `PROMETHEUS_RETENTION_SIZE` видали з Coolify env — новий Compose їх не використовує.
Retention Prometheus заданий безпосередньо як `30d`, щоб старе env-значення 15d не скоротило історію.
Старий `grafana-data` volume новий стек не використовує; не видаляй його, якщо потрібні попередні налаштування.

## Сповіщення та фонові правила

У Beszel можна налаштувати пороги CPU/RAM/диска та канали сповіщень через вебінтерфейс.
Email/Telegram самостійно не надсилаються, доки відповідний канал не налаштовано.

У Prometheus залишені 8 правил: недоступний exporter, тривалий високий CPU/load, нестача RAM, місця/inode, OOM kill та наближення контейнера до ліміту RAM.
Вони виконуються у фоні; у Beszel автоматично не відображаються. Для перегляду — внутрішній Prometheus UI `/alerts` або API `/api/v1/alerts`.
Alertmanager і зовнішня доставка повідомлень Prometheus не налаштовані.

## Перегляд детальних метрик Prometheus

Prometheus не має публічного порту чи домену. У Coolify terminal його контейнера можна перевірити targets:

```bash
wget -qO- http://127.0.0.1:9090/api/v1/targets
```

Для тимчасового UI на сервері дізнайся назву контейнера та його IP у мережі стека:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}'
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' PROMETHEUS_CONTAINER_NAME
```

Зі свого компʼютера відкрий SSH-тунель до однієї з цих внутрішніх IP:

```bash
ssh -N -L 9090:PROMETHEUS_CONTAINER_IP:9090 user@server
```

Після цього відкрий `http://localhost:9090`. У **Status → Targets** усі три targets мають бути Up.
Для історії вибирай Graph і часовий діапазон за останні 30 днів; приклад CPU:

```promql
100 * (1 - avg(rate(node_cpu_seconds_total{job="node",mode="idle"}[5m])))
```

## Дані та ресурси

| Volume | Що зберігає |
| --- | --- |
| `beszel-data` | База Beszel, користувачі, історія, приватний ключ Hub |
| `beszel-agent-data` | Постійні дані агента / fingerprint |
| `beszel-socket` | Спільний Unix socket Hub → Agent |
| `prometheus-data` | Повна історія метрик Prometheus за 30 днів |

Звичайний redeploy зберігає volumes. Не використовуй `docker compose down -v` і не видаляй volumes у Coolify, якщо потрібна історія.
Роби резервні копії бази Beszel та даних Prometheus; retention не замінює backup.

Ліміти RAM: Hub 256 MiB, Agent 128 MiB, Prometheus 1 GiB, cAdvisor 512 MiB, Node Exporter 128 MiB.
Це верхні межі, не резервування. Реальне споживання залежить від кількості контейнерів та метрик.
Логи кожного сервісу обмежені трьома файлами по 10 MB. Версії образів зафіксовані.

`BESZEL_USER_EMAIL` / `BESZEL_USER_PASSWORD` створюють початкового користувача лише в новій базі.
Для наявного користувача змінюй пароль у Beszel UI; зміна env сама пароль не скидає.
Збережи приватний ключ Hub у `beszel-data`; якщо він зміниться, онови `BESZEL_AGENT_KEY`.

## Перевірка конфігурацій

```bash
sh scripts/check.sh
```

Скрипт перевіряє всі чотири варіанти Compose (setup/full, Coolify/local), конфігурацію Prometheus, його 8 правил та сценарії алертів.
Потрібні Docker Compose й запущений Docker daemon; якщо встановлено локальний `promtool`, daemon для цих перевірок не потрібен.
Тестові env-значення використовуються лише для валідації, сервіси не запускаються.

## Запуск на Linux без Coolify

```bash
cp .env.example .env
# Заповни URL, email, пароль і NODE_EXPORTER_HOST у .env.
docker compose -f compose.setup.yml -f compose.local.yml up -d
```

Beszel доступний лише на `127.0.0.1:8090`. Для віддаленого доступу:

```bash
ssh -N -L 8090:127.0.0.1:8090 user@server
```

Відкрий `http://localhost:8090`, додай систему з Host `/beszel_socket/beszel.sock` та скопіюй публічний ключ у `BESZEL_AGENT_KEY` у `.env`.
Потім у тій самій папці, зі збереженням Compose project name:

```bash
docker compose -f docker-compose.yml -f compose.local.yml up -d
```

У Coolify `compose.local.yml` не використовуй.

## Якщо щось не працює

- **Agent / система Down:** перевір публічний ключ, Host `/beszel_socket/beszel.sock` у Beszel та однаковий volume `beszel-socket` у Hub і Agent. Після зміни env зроби redeploy.
- **`bind: cannot assign requested address` у Node Exporter:** перевір IPv4 `docker0` та `NODE_EXPORTER_HOST`.
- **Node target Down:** перевір адресу `node-host`, listener Node Exporter і firewall між Docker-мережею та хостом.
- **Помилка монтування конфігурацій:** увімкни Preserve Repository; перевір, чи існують `prometheus/prometheus.yml` та `prometheus/alerts.yml` як файли.
- **Немає метрик контейнерів:** перевір Docker socket, rootful Docker, DockerRootDir і логи агента/cAdvisor.
- **No available server:** перевір healthy у Hub та привʼязку домену до **beszel:8090**.
- **Недостатньо місця:** перевір обсяг TSDB і бази Beszel; збільшуй диск, якщо потрібно зберігати повні 30 днів.

Документація: [Beszel](https://beszel.dev/guide/getting-started), [змінні середовища](https://beszel.dev/guide/environment-variables), [політика зберігання Beszel 0.21.0](https://github.com/henrygd/beszel/blob/v0.21.0/internal/records/records_deletion.go), [Coolify Docker Compose](https://coolify.io/docs/applications/builds/docker-compose).
