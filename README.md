# mashninski-site-strymy

Бот, які сочыць за беларускімі стрымерамі на Twitch для старонкі
[mashninski.com/strymy](https://mashninski.com/strymy): хто зараз у эфіры, калі быў
апошні стрым, колькі ён доўжыўся, статыстыка па месяцах і хроніка эфіраў.

## Як гэта працуе

```
Cloudflare Worker  →  GitHub Actions: collect.py  →  ветка data  →  сайт
cron раз на 10 хв     пытае Twitch API, лічыць       JSON-файлы     чытае праз
+ Twitch EventSub     сесіі, статыстыку, архіў                      GitHub API
```

- **`collect.py`** раз на дзесяць хвілін пытае Twitch, хто ў эфіры, лавіць момант
  заканчэння трансляцыі, шукае запіс (VOD) і па ім удакладняе час эфіру. Лічыць
  статыстыку па месяцах і гадзінах сутак, гадзіны прагляду (гледачы × час эфіру)
  і збірае архіў месяцаў.
- **Даныя** — JSON-файлы ў асобнай галіне [`data`](../../tree/data): стан, статыстыка
  і архіў. Бот камітае туды толькі пры зменах, а сайт чытае іх праз GitHub API.
  Галіна `main` застаецца чыстай: у ёй толькі код і рэестр стрымераў `streamers.json`.
- **`worker/`** — Cloudflare Worker-«будзільнік». Запускае збор кожныя 10 хвілін
  і адразу, калі Twitch EventSub паведамляе пра пачатак або канец эфіру. Ён жа сам
  падпісвае і адпісвае стрымераў ад EventSub і раз на дзве гадзіны запускае
  суседні бот навін [mashninski-site-naviny](https://github.com/mashninski/mashninski-site-naviny).

Базы даных няма: стан — гэта файлы ў git, а GitHub Actions і Cloudflare Workers
укладаюцца ў бясплатныя ліміты.

## Стэк

Python 3.12 · Twitch Helix API і EventSub · GitHub Actions · Cloudflare Workers (JavaScript) · Wrangler

## Запуск

```bash
pip install requests
TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=... python collect.py

cd worker
npm install
npm test          # тэсты Worker’а
npm run deploy    # выкладка ў Cloudflare
```

Ключы Twitch захоўваюцца ў GitHub Secrets, сакрэты Worker’а — у Cloudflare
(`wrangler secret put`). У рэпазіторыі іх няма.

## Як дадаць стрымера

Дадаць запіс у `streamers.json` (уручную або праз адмінку сайта). Наступны прагон
пачне збіраць даныя, а Worker сам падпіша канал на EventSub.
