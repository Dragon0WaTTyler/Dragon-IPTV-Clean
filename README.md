# Dragon IPTV Clean

This repository is a clean generated mirror of the `hot-dodo` source playlist set.

What it does:

- Fetches raw `FIW_17*.m3u` files from [`hot-dodo`](https://github.com/mesbahikarim63-commits/hot-dodo) during GitHub Actions.
- Keeps live TV streams, removes obvious movie and series entries, and preserves playback authentication parameters.
- Classifies Arabic and English using explicit language markers and country prefixes instead of generic words such as `TV`, `news`, or `sport`.
- Splits the output into Arabic-only and English-only catalogs with prioritized news and documentary channels.
- Checks priority, news, and documentary streams during GitHub Actions and promotes a reachable alternate when the primary URL fails.
- Commits generated `dist/` files back into this repository when the generated outputs change.

The pipeline processes all source files when `source_selection.max_files` is `0` (or only the newest configured number), deduplicates quality variants, and keeps alternate URLs in each JSON catalog entry.

Run the regression tests with:

```bash
python3 -m unittest discover -s tests -v
```

Run a local build without network health checks with:

```bash
python3 scripts/build_iptv_dist.py --source-dir tmp/iptv-source --output-dir dist
```

Add `--health-check` to use the same stream validation enabled in GitHub Actions. The checker is bounded by `config/iptv_rules.json`: it tests at most three URLs per selected channel with 40 workers and a five-second request timeout. It only accepts public HTTP/HTTPS targets, validates redirects, and never removes a channel after a single failed run. Health results are included in each catalog record and manifest.

Current limitations:

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
