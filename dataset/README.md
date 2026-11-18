---
license: other
license_name: common-crawl-terms-of-use
license_link: https://commoncrawl.org/terms-of-use
pretty_name: Common Crawl domain link history
tags:
- web-graph
- backlinks
- common-crawl
size_categories:
- 1B<n<10B
---

# Common Crawl domain link history

Which domains link to which, across every Common Crawl domain-level web graph since January 2018, with the first and
last release each link was seen in.

Common Crawl publishes one domain graph per release, each covering a few months, with links sorted by the linking
domain. This dataset merges the releases into one history sorted by the linked domain, so you can ask "which domains
have ever linked to example.com, and when" with a few range reads instead of downloading every release.

Built from Common Crawl's public web graphs (https://commoncrawl.org/web-graphs). All credit for the crawl and the
graphs goes to Common Crawl; use of this data is subject to their terms of use (https://commoncrawl.org/terms-of-use).
Common Crawl samples the web, so a missing link means "not seen in these crawls", never "does not exist".

## Files

- `releases.tsv`: one line per release folded in: index, release name, domains, links, date added, then the
  sha256 and byte size of `domains.txt` as that release left it. A link's first and last values below are these
  indexes.
- `domains.txt`: every domain ever seen, in reversed notation (`com.example`, `uk.co.example`), one a line, in byte
  order. A domain's id is its line number, counting from 0. `domains.txt.idx` gives, every 4,096 lines, the line
  block number, its byte offset and its first name, so one domain can be found with a single range read.
- `history/h-NNNNN.bin`: the link history in shards. Each shard: the 4 bytes `LGH1`, a little-endian 64-bit record
  count, then records. A record: target domain id (LEB128 varint), number of linking domains (varint), then for each
  linking domain, ascending: the id gap from the previous one (varint; the first is the id itself), one byte for the
  first release index and one for the last.
- `history/history.idx`: tab-separated shard number, byte offset and first target id, at each shard start and about
  every megabyte, for range reads.

A reference reader (`lg lookup`) and the builder are at https://github.com/ahon-digital/ahon-linkgraph.

## Updates

A new Common Crawl release is folded in within a few hours of its publication. Releases are added strictly in date
order; the history is rewritten in place and older file versions are removed.

## Questions and removal requests

Open a discussion on this dataset's Community tab.
