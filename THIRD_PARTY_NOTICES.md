# External design references

No upstream implementation code or verbatim README/JPL passage is distributed in this release. The provenance directory contains original interpretation notes with citations. The project's noncommercial license does not relicense any external project.

| Reference | Pinned identity | Licensing observed upstream |
|---|---|---|
| [micrograd](https://github.com/karpathy/micrograd/tree/7bc720e951fe422b8f8814aa5aa1b64121d26b4c) | `7bc720e951fe422b8f8814aa5aa1b64121d26b4c` | MIT, copyright 2020 Andrej Karpathy; upstream LICENSE inspected |
| [llm.c](https://github.com/karpathy/llm.c/tree/f1e2ace651495b74ae22d45d1723443fd00ecd3a) | `f1e2ace651495b74ae22d45d1723443fd00ecd3a` | MIT, copyright 2024 Andrej Karpathy; upstream LICENSE inspected |
| [autoresearch](https://github.com/karpathy/autoresearch/tree/228791fb499afffb54b46200aca536f79142f117) | `228791fb499afffb54b46200aca536f79142f117` | Pinned README declares MIT; no separate LICENSE, LICENSE.md or LICENSE.txt found at that revision |
| [JPL State Analysis](https://www.jpl.nasa.gov/work-with-us/technology-system/mds/system-engineering/) | Mutable official page; collection metadata in SOURCES.json | No redistribution license established; original quotation omitted |

The original local excerpt hashes remain as historical metadata. New archived hashes identify release-authored notes, not copies of upstream passages. Design inspiration is not implementation verification, independent reproduction or institutional certification.

# Bundled dependencies

The StonX chart (`stonx_chart/web`) bundles two runtime dependencies into its built page (`fancy-canvas` is a dependency of `lightweight-charts`, and the chart's band drawing uses its types). Unlike the references above, this is distributed code, used under its own license.

| Dependency | Version | License | Copyright |
|---|---|---|---|
| [lightweight-charts](https://github.com/tradingview/lightweight-charts) | `5.2.1` (pinned exactly) | Apache-2.0 | Copyright 2023 TradingView, Inc. |
| [fancy-canvas](https://github.com/tradingview/fancy-canvas) | `2.1.0` (pinned exactly) | MIT | Copyright (c) 2019 TradingView, Inc |

The `lightweight-charts` copyright line is copied from the `LICENSE` file shipped in the 5.2.1 package. The `fancy-canvas` 2.1.0 npm package declares MIT in its `package.json` but does not ship a `LICENSE` file, so its copyright line is copied from the `LICENSE` file in the upstream repository. The production build keeps the libraries' license header comments in the bundled JavaScript (`build.rolldownOptions.output.comments.legal` in `stonx_chart/web/vite.config.ts`). Those headers do not carry the license texts, so the built page also ships [`third-party-licenses.txt`](stonx_chart/web/public/third-party-licenses.txt). It holds the full Apache-2.0 text from the `lightweight-charts` package and the `fancy-canvas` MIT notice, and the page links to it from its status bar. `stonx_chart/web/tests/licenses.test.ts` fails if a version bump leaves it stale. The `lightweight-charts` 5.2.1 package ships no `NOTICE` file. The chart keeps the library's attribution logo enabled (`layout.attributionLogo: true`) and it must stay enabled.
