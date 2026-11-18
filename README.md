# ahon-linkgraph

Builds the Common Crawl domain link history published at https://huggingface.co/datasets/ahon-digital/linkgraph:
for every domain, which domains have linked to it and the first and last Common Crawl release each link was seen in,
from January 2018 on.

- `src/lg.c`: the builder (standard C, no libraries). See its header for the commands and the file format.
- `scripts/run.sh`: one step: fold the next release, in date order, into the history.
- `scripts/hf.py`, `scripts/releases.py`: Hugging Face upload/download and Common Crawl's release order (Python
  standard library only).
- `tests/test_lg.py`: the builder against a plain-Python reference, plus the refusals.

The data comes from Common Crawl's public web graphs (https://commoncrawl.org/web-graphs) and is subject to Common
Crawl's terms of use (https://commoncrawl.org/terms-of-use).

Copyright Ahon Digital. All rights reserved.
