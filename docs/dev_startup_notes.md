# Dev startup notes

## npm install нужен перед первым `npm run dev`

`CLAUDE.md` говорит запускать UI так:

```bash
npm --prefix ui/workspace run dev
```

Но если `ui/workspace/node_modules/` не существует (свежий клон или после `git clean`),
команда падает с `sh: vite: command not found`.

Нужно сначала установить зависимости:

```bash
npm --prefix ui/workspace install
npm --prefix ui/workspace run dev
```

`npm install --prefix` не работает (флаг не поддерживается в этой форме) — нужно
именно `npm --prefix ... install`.

CI этой проблемы не видит, потому что гоняет только `npm run build`, а не `dev`.
