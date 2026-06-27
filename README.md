# Dragon IPTV Clean

This repository is a clean generated mirror of the `krimo.-Iptv` source playlist set.

What it does:

- Fetches raw `FIW_17*.m3u` files from [`krimo.-Iptv`](https://github.com/mesbahikarim03-svg/krimo.-Iptv) during GitHub Actions.
- Cleans the playlists and splits the output into Arabic-only and English-only catalogs.
- Commits generated `dist/` files back into this repository when the generated outputs change.

What it does not do in V0:

- No EPG handling.
- No player logic.
- No notifications.
- No proxy or restreaming.
- No `ffmpeg`.
- No torrent, magnet, or `acestream` support.
- No AI classification.

Output layout:

- Arabic outputs live in `dist/arabic/`.
- English outputs live in `dist/english/`.
- There are no mixed root-level `dist/*.m3u` files.

Per language folder you will find:

- `manifest.json`
- `dragon_iptv_catalog.json`
- language main playlist: `arabic.m3u` or `english.m3u`
- `news.m3u`
- `documentary.m3u`
- `sports.m3u`
- `netflix.m3u`

Notes:

- `netflix.m3u` means movies, series, and cinema-style channels. It does not mean the official Netflix service.
- The builder keeps only Arabic and English channels and drops promo/demo/info/welcome entries, unsupported protocols, and non-target languages by default.
- iOS should consume this clean repo after verification.

Build entry point:

- [`scripts/build_iptv_dist.py`](./scripts/build_iptv_dist.py)

Rules:

- [`config/iptv_rules.json`](./config/iptv_rules.json)
