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

The StonX chart (`stonx_chart/web`) bundles one runtime dependency into its built page. Unlike the references above, this is distributed code, used under its own license.

| Dependency | Version | License | Copyright |
|---|---|---|---|
| [lightweight-charts](https://github.com/tradingview/lightweight-charts) | `5.2.1` (pinned exactly) | Apache-2.0 | Copyright 2023 TradingView, Inc. |

The copyright line is copied from the `LICENSE` file shipped in the `lightweight-charts` 5.2.1 package. The chart keeps the library's attribution logo enabled (`layout.attributionLogo: true`) and it must stay enabled.
