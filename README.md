# Beszel + Prometheus для Ubuntu та Coolify

Beszel — основний вебінтерфейс для CPU, RAM, дисків, мережі та Docker-контейнерів.
Prometheus паралельно збирає метрики Node Exporter і cAdvisor у фоні, незалежно від Beszel.
Окремий контейнер `telegram-bot` читає свіжі метрики Beszel та надсилає критичні сповіщення.
Обидві системи зберігають історію за останні **30 днів** у постійних Docker volumes.

```text
Браузер → HTTPS / Coolify Proxy → Beszel Hub:8090
                                    └─ Unix socket → Beszel Agent → хост і Docker

Prometheus:9090 (внутрішній) ─┬─ docker0 IP:9100 → Node Exporter → хост
                            └─ cadvisor:8080  → cAdvisor → Docker-контейнери

Telegram ← telegram-bot → внутрішній API Beszel Hub
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

Якщо DockerRootDir відрізняється від `/var/lib/docker`, заміни обидві частини монтування cAdvisor `/var/lib/docker:/var/lib/docker:ro` на фактичний шлях у **двох** файлах: `compose.setup.yml` та `docker-compose.yml`.
Наприклад, для `/mnt/docker` використовуй `/mnt/docker:/mnt/docker:ro`. Шляхи мають бути явними: Coolify перевіряє volumes до підстановки env і може відхиляти `${...}`. Змінна `DOCKER_DATA_ROOT` більше не використовується.
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
4. Переконайся, що в Git є `prometheus/Dockerfile`, `prometheus/prometheus.yml` та `prometheus/alerts.yml`. Coolify збирає образ Prometheus із цими конфігураціями; **Preserve Repository** для цього більше не потрібен. Під час збірки `promtool` перевіряє конфігурацію та правила.
5. У **Environment Variables** встанови:

   | Змінна | Значення |
   | --- | --- |
   | `BESZEL_APP_URL` | `https://monitor.example.com` — реальний URL без внутрішнього `:8090` |
   | `BESZEL_USER_EMAIL` | Email початкового адміністратора |
   | `BESZEL_USER_PASSWORD` | Власний довгий випадковий пароль, без зовнішніх лапок |
   | `NODE_EXPORTER_HOST` | IPv4 інтерфейсу `docker0` |

6. Спрямуй DNS на сервер Coolify proxy. У **Domains** вибери сервіс **beszel** і задай `https://monitor.example.com:8090`.
   `:8090` тут задає порт контейнера; браузер відкриває звичайний `https://monitor.example.com`.
7. Натисни **Deploy**, відкрий Beszel і увійди з email та паролем із env.
8. Натисни **Add System**, задай назву сервера та **Host / IP** `/beszel_socket/beszel.sock`. Порт можна залишити стандартним `45876`: для Unix socket він не використовується.
9. Скопіюй публічний ключ із цієї форми (рядок `ssh-ed25519 ...`), збережи систему та додай у Coolify `BESZEL_AGENT_KEY` із цим ключем. Не підставляй token або приватний ключ.
10. Перевір збережені `NODE_EXPORTER_HOST` та, за потреби, `DOCKER_ENDPOINT` (див. `.env.example`), а також явний шлях DockerRootDir у монтуваннях cAdvisor в обох Compose-файлах.
11. У **тій самій** Coolify application зміни **Docker Compose Location** на `/docker-compose.yml`, збережи та перезавантаж конфігурацію з Git.
    Переконайся, що домен лишився привʼязаним до сервісу **beszel:8090**.
12. Перед фінальним деплоєм встанови `TELEGRAM_BOT_TOKEN` та `TELEGRAM_CHAT_ID` за інструкцією нижче. У `compose.setup.yml` бот ще не запускається.
13. Натисни **Deploy**. Залиш звичайний режим Compose, **Raw mode не вмикай**. Не додавай домени для агента, бота, Prometheus чи експортерів.

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

### Telegram-бот

Бот запускається у власному контейнері без відкритих портів. Він опитує Beszel API кожні 30 секунд і перевіряє останній запис типу `1m`, а не усереднені місячні дані.

| Метрика | Критичний поріг | Повідомлення про відновлення |
| --- | --- | --- |
| Загальне CPU сервера | **>95%** | ≤92% |
| GPU, кожна окремо | **>95%** | ≤92% |
| RAM сервера | **>70%** | ≤67% |
| VRAM, кожна GPU окремо | **>70%** від `memory used / memory total` | ≤67% |
| Основна батарея ноутбука | **<35%** | ≥38% |

Перше перевищення спричиняє повідомлення одразу після отримання свіжого запису. У Beszel запис оновлюється приблизно раз на хвилину, тому це не миттєва перевірка коротких піків.
Сповіщення про батарею працює і під час заряджання, якщо заряд нижчий за 35%. Батареї мишок та інших периферійних пристроїв не використовуються.
Поки поріг перевищений, бот повторює сповіщення раз на 30 хвилин (`TELEGRAM_ALERT_REPEAT_SECONDS=1800`). Різні GPU та різні метрики мають окремий стан.
Запас 3 відсоткові пункти для відновлення запобігає потоку повідомлень поблизу порога. Якщо значення вже нижче критичного, але ще в цьому запасі, повтори не надсилаються.
Стан зберігається в `telegram-bot-data`, тому звичайний restart/redeploy не повторює вже надіслану тривогу.
Невдала доставка повторюється; Telegram rate limits враховуються. Рідкісний збій між доставкою та записом стану може призвести до дублювання.

Налаштування:

1. Створи бота через [@BotFather](https://t.me/BotFather) і додай token у **Coolify → Environment Variables → `TELEGRAM_BOT_TOKEN`**.
2. Відкрий приватний чат із ботом і натисни **Start**, або додай його в потрібну групу з дозволом писати повідомлення.
3. Дізнайся ID потрібного чату через Telegram Bot API `getUpdates` після повідомлення боту. Для груп повідомлення має бути видиме боту, наприклад команда з його username. Збережи значення `message.chat.id` у `TELEGRAM_CHAT_ID` (для груп воно зазвичай відʼємне).
4. За замовчуванням бот використовує `BESZEL_USER_EMAIL/PASSWORD`. За потреби створи окремого користувача Beszel з read-only доступом до потрібної системи та задай `TELEGRAM_BESZEL_EMAIL/PASSWORD`.
   Якщо пароль користувача вже змінений у UI, тут має бути актуальний пароль, а не старе початкове значення.
5. За замовчуванням перевіряються всі доступні цьому користувачу системи. Для одного ноутбука задай `TELEGRAM_BESZEL_SYSTEM_ID` — ID його запису в API Beszel (15 символів).
6. Зроби redeploy повного `docker-compose.yml`. Coolify збере образ бота з `telegram-bot/Dockerfile`; зовнішні Python-пакети не потрібні.

На хості потрібен вихідний HTTPS-доступ до `api.telegram.org:443`. Бот надсилає звичайні повідомлення з `disable_notification=false`; фактичний звук/push залежить також від налаштувань Telegram на телефоні.
Token і пароль не записуються у файл стану й не виводяться в повідомленнях помилок.

**Команда `/status`** повертає звіт для кожної доступної системи (або лише `TELEGRAM_BESZEL_SYSTEM_ID`, якщо заданий):

- останні значення CPU, RAM, а за наявності — GPU, VRAM кожної відеокарти та батареї;
- середні за останні **5 хвилин** та **1 годину**;
- час останнього виміру в UTC і позначку, якщо запис застарілий або сервер недоступний.

У групі можна писати `/status@ІмʼяТвогоБота`. Запити приймаються лише з `TELEGRAM_CHAT_ID`.
Бот перевіряє вхідні команди приблизно кожні 2 секунди; час відповіді залежить також від API Beszel/Telegram.
Для команд використовується Telegram `getUpdates`: для одного token має працювати один екземпляр бота, без активного webhook чи іншого споживача updates.
У журналі `Telegram: HTTP 409` означає конфлікт іншого споживача або webhook.

Середні — арифметичні за доступними записами Beszel типу `1m` у відповідному часовому вікні.
Історія читається з Hub на запит, тому перезапуск бота не обнуляє вікна.
У дужках біля кожного середнього показана кількість вимірів: для повного вікна очікується приблизно 5 та 60.
Якщо історія коротша або були пропуски, середнє обчислюється лише за наявними вимірами; відсутні значення не замінюються нулями.
`н/д` означає, що для метрики немає даних. «Останнє» — останній запис Beszel, який зазвичай оновлюється раз на хвилину.
Позиція обробки команд зберігається в `telegram-bot-data/commands.json`; команди старші 5 хвилин пропускаються.
Невдала доставка повторюється, доки команда не застаріла. Після збою між доставкою й записом позиції можливий повтор відповіді.

Якщо відсутня GPU/VRAM/батарея, система Down або записи старші трьох хвилин, бот не підставляє нулі й не надсилає хибне відновлення.
Після двох хвилин безперервної відсутності даних він повідомляє про проблему; повтор — раз на 30 хвилин.
Healthcheck бота підтверджує роботу циклу перевірки, а доступність джерела/Telegram контролюється окремо повідомленнями та логами.
При вимкненому ноутбуку або відсутньому інтернеті бот на тому самому ноутбуку не зможе відправити сповіщення.

### Доступ до GPU та батареї ноутбука

Агенту додано read-only mount `/sys:/sys`. Батарея читається Linux із `/sys/class/power_supply`; її заряд має також зʼявитися в Beszel.
Перевір на хості: `ls /sys/class/power_supply` та `cat /sys/class/power_supply/BAT*/capacity`.

**AMD:** автоматичне визначення sysfs увімкнене; за потреби задай `BESZEL_GPU_COLLECTOR=amd_sysfs`. Дані залежать від драйвера ядра.

**NVIDIA:** встанови NVIDIA driver та NVIDIA Container Toolkit на хості, налаштуй Docker runtime `nvidia`, перевір `nvidia-smi`. Потім у Coolify встанови:

```dotenv
BESZEL_AGENT_IMAGE=henrygd/beszel-agent-nvidia:0.21.0
BESZEL_AGENT_RUNTIME=nvidia
BESZEL_GPU_COLLECTOR=nvidia-smi
```

Compose уже задає `NVIDIA_VISIBLE_DEVICES=all` та `NVIDIA_DRIVER_CAPABILITIES=utility`; після redeploy агент отримає засоби збору NVIDIA.
Для стандартного агента залишай `BESZEL_AGENT_RUNTIME=runc`.

Для Intel/iGPU доступність utilization і окремої VRAM залежить від GPU/драйвера; спільна RAM не видається за dedicated VRAM.
Для повного збору Intel може знадобитися `henrygd/beszel-agent-intel:0.21.0`, device `/dev/dri` та додаткові дозволи — див. [GPU monitoring Beszel](https://beszel.dev/guide/gpu).
Поки Beszel не показує реальну utilization і VRAM, бот повідомлятиме про відсутність цих метрик. На цільовому обладнанні це потрібно перевірити після деплою.

### Інші правила

У Beszel можна додатково налаштувати пороги CPU/RAM/диска й інші канали через UI. Щоб уникнути дублів, не створюй там ті самі Telegram-алерти паралельно з контейнером бота.

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
| `telegram-bot-data` | Стан тривог та часу повторів Telegram-бота |
| `prometheus-data` | Повна історія метрик Prometheus за 30 днів |

Звичайний redeploy зберігає volumes. Не використовуй `docker compose down -v` і не видаляй volumes у Coolify, якщо потрібна історія.
Роби резервні копії бази Beszel та даних Prometheus; retention не замінює backup.

Ліміти RAM: Hub 256 MiB, Agent 128 MiB, Telegram-бот 128 MiB, Prometheus 1 GiB, cAdvisor 512 MiB, Node Exporter 128 MiB.
Це верхні межі, не резервування. Реальне споживання залежить від кількості контейнерів та метрик.
Логи кожного сервісу обмежені трьома файлами по 10 MB. Версії образів зафіксовані.

`BESZEL_USER_EMAIL` / `BESZEL_USER_PASSWORD` створюють початкового користувача лише в новій базі.
Для наявного користувача змінюй пароль у Beszel UI; зміна env сама пароль не скидає.
Збережи приватний ключ Hub у `beszel-data`; якщо він зміниться, онови `BESZEL_AGENT_KEY`.

## Перевірка конфігурацій

```bash
sh scripts/check.sh
```

Скрипт перевіряє всі чотири варіанти Compose (setup/full, Coolify/local), логіку Telegram-бота, конфігурацію Prometheus, його 8 правил та сценарії алертів. Тести не надсилають повідомлень у реальний Telegram.
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

Відкрий `http://localhost:8090`, додай систему з Host `/beszel_socket/beszel.sock` та скопіюй публічний ключ у `BESZEL_AGENT_KEY` у `.env`. Заповни також `TELEGRAM_BOT_TOKEN` і `TELEGRAM_CHAT_ID`.
Потім у тій самій папці, зі збереженням Compose project name:

```bash
docker compose -f docker-compose.yml -f compose.local.yml up -d
```

У Coolify `compose.local.yml` не використовуй.

## Якщо щось не працює

- **Agent / система Down:** перевір публічний ключ, Host `/beszel_socket/beszel.sock` у Beszel та однаковий volume `beszel-socket` у Hub і Agent. Після зміни env зроби redeploy.
- **`bind: cannot assign requested address` у Node Exporter:** перевір IPv4 `docker0` та `NODE_EXPORTER_HOST`.
- **Node target Down:** перевір адресу `node-host`, listener Node Exporter і firewall між Docker-мережею та хостом.
- **Помилка монтування `/etc/prometheus/prometheus.yml`:** онови Compose із Git: конфігурації тепер включені в образ через `prometheus/Dockerfile`, а Prometheus монтує лише volume даних `/prometheus`. Прибери старі монтування конфігурацій зі Storage у Coolify, якщо додавав їх вручну, та зроби redeploy. Видаляти `prometheus-data` не потрібно.
- **`Invalid volume source: contains forbidden character '${'`:** використовуй явні шляхи у volumes. У cAdvisor має бути `/var/lib/docker:/var/lib/docker:ro` (або фактичний DockerRootDir з обох боків) в обох Compose-файлах. Збережи виправлення в Git і перезавантаж Compose у Coolify.
- **Немає метрик контейнерів:** перевір Docker socket, rootful Docker, DockerRootDir і логи агента/cAdvisor.
- **No available server:** перевір healthy у Hub та привʼязку домену до **beszel:8090**.
- **Недостатньо місця:** перевір обсяг TSDB і бази Beszel; збільшуй диск, якщо потрібно зберігати повні 30 днів.
- **Telegram не надходить:** перевір token/chat ID, Start у чаті, права бота в групі та вихідний HTTPS. У Runtime Logs помилки `Telegram: HTTP 401/403/429` означають проблему з token/дозволами/лімітами.
- **GPU/VRAM або батарея недоступні:** спочатку переконайся, що їх бачить Beszel; для NVIDIA перевір Toolkit/runtime, для батареї — `/sys/class/power_supply`.

Документація: [Beszel](https://beszel.dev/guide/getting-started), [змінні середовища](https://beszel.dev/guide/environment-variables), [політика зберігання Beszel 0.21.0](https://github.com/henrygd/beszel/blob/v0.21.0/internal/records/records_deletion.go), [Coolify Docker Compose](https://coolify.io/docs/applications/builds/docker-compose).
