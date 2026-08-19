# Deploying the coldscreen site to Cloudflare Pages

This directory is a fully static site: plain HTML, CSS, and a small vanilla JS file. There is no build step and no framework.

## Cloudflare Pages settings (Git integration)

- Framework preset: None
- Production branch: main
- Root directory: `website`
- Build command: leave empty
- Build output directory: `/` (the root directory itself; no further output directory is needed)

No environment variables, no Pages Functions, no redirects, no headers file.

## Direct upload alternative

From the repository root:

```bash
npx wrangler pages deploy website
```

## Notes

- The only external request the page makes is to Google Fonts (fonts.googleapis.com and fonts.gstatic.com). There are no analytics and no other third-party assets.
- Assets are referenced with relative paths (`css/site.css`, `js/site.js`), so the site also works from a subpath or a preview URL.
