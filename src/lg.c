// lg - Ahon link graph builder. Turns Common Crawl's domain-level web graphs into one growing link history:
// for every domain, which domains have linked to it, and the first and last release each link was seen in.
// Our own code, standard C only (no outside libraries). Built for a 16 GB / 14 GB-disk runner: the link file is
// streamed (twice, or once per part when memory is short) instead of stored, and the history is streamed in and out shard by shard.
//
//   lg names OUT POS                stdin: Common Crawl vertices "id<TAB>rev_name[<TAB>hosts]" -> OUT, the distinct
//                                   names in byte order, one a line, and POS (u32 per release id: its line in OUT).
//                                   Accepts any input order. A name listed twice (Common Crawl's 2019-may-jun-jul lists
//                                   no.hordaland twice) is one domain: both ids get the same position. More than
//                                   1000 + N/1000 duplicates means a broken file and stops. Refuses ids that skip.
//   lg dictmerge OLD|- REL NEW MAP_OLD MAP_REL
//                                   Union of two sorted name lists -> NEW (+ NEW.idx), and u32 maps old->new, rel->new.
//   lg inbound N E MAP_REL POS OUT CMD [PART_LINKS]
//                                   Runs CMD (it must print the release's "from<TAB>to" edges, sorted by from) once to
//                                   count, then once per part to place: "who links to X" in ~16 bytes a node + 4 a
//                                   link of the part. A part is a run of targets holding at most PART_LINKS links
//                                   (default E: one part; a target with more links than that is a part of its own).
//                                   Every placing pass must read the counting pass's edges exactly, or it stops.
//                                   POS maps release ids to distinct-name positions, MAP_REL positions to global ids.
//                                   OUT: records (target, count, sources) in global ids, targets and sources ascending;
//                                   a link repeated because two ids share a name is written once.
//   lg merge REL_INDEX PREVCMD|- PREVIDX|- MAP_OLD REL OUTDIR SHARD_KB [ON_SHARD]
//                                   Folds a release (lg inbound OUT) into the history. PREVCMD prints the previous
//                                   history's shards in order; PREVIDX is that history's index. REL_INDEX must be
//                                   exactly one past the release PREVIDX records (0 with no history), and every shard
//                                   PREVIDX lists must arrive whole, or the merge stops. Writes OUTDIR/h-NNNNN.bin + OUTDIR/history.idx. If
//                                   ON_SHARD is given, it runs as "ON_SHARD <path>" after each shard closes (upload it,
//                                   delete it), so the disk never holds the whole history; a failure stops the merge.
//   lg dump DIR NAMES               Every link as "target<TAB>source<TAB>first<TAB>last" (tests).
//   lg lookup DIR NAMES DOMAIN      One domain's linking domains with first and last release (reference reader).
//
// History shard: "LGH1", u64 record count, records. Record: varint target (absolute global id), varint count, then
// count x (varint source delta, first-release byte, last-release byte); the first source delta is the absolute id.
// history.idx: first line "#release<TAB>N" (the last release folded in), then "shard<TAB>offset<TAB>first_target" at
// every shard start and about every 1 MB, for range reads, then "#shard<TAB>n<TAB>records<TAB>bytes" for each shard.
// A merge checks every shard it reads against those lines, so a shard cannot shrink without the index noticing.
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>

static void die(const char *m) { fprintf(stderr, "lg: %s\n", m); exit(2); }
// Strict decimal: digits only, no sign, no overflow, at most max. Anything else stops.
static uint64_t num(const char *s, uint64_t max, const char *what) {
  uint64_t v = 0;
  if (!s || !*s) { fprintf(stderr, "lg: %s is empty\n", what); exit(2); }
  for (const char *p = s; *p; p++) {
    if (*p < '0' || *p > '9' || v > (UINT64_MAX - 9) / 10) { fprintf(stderr, "lg: %s is not a number in range\n", what); exit(2); }
    v = v * 10 + (uint64_t)(*p - '0');
  }
  if (v > max) { fprintf(stderr, "lg: %s is out of range\n", what); exit(2); }
  return v;
}
// Splits a tab-separated index line into at most n strict numbers; returns how many fields it had.
static int fields(char *line, uint64_t *out, int n, uint64_t max, const char *what) {
  { size_t L = strlen(line); if (L && line[L - 1] == '\n') line[L - 1] = 0; }   /* only the newline: a \r or anything else fails num() */ int k = 0; char *t = line;
  for (;;) {
    char *tab = strchr(t, '\t'); if (tab) *tab = 0;
    if (k == n) return n + 1;
    out[k++] = num(t, max, what);      // num() refuses an empty field
    if (!tab) return k;
    t = tab + 1;
  }
}
static void *xmalloc(size_t n) { void *p = malloc(n ? n : 1); if (!p) die("out of memory"); return p; }
static void *xcalloc(size_t n, size_t s) { void *p = calloc(n ? n : 1, s); if (!p) die("out of memory"); return p; }
static void *xrealloc(void *p, size_t n) { p = realloc(p, n ? n : 1); if (!p) die("out of memory"); return p; }
static FILE *xopen(const char *p, const char *m) { FILE *f = fopen(p, m); if (!f) { fprintf(stderr, "lg: %s: %s\n", p, strerror(errno)); exit(2); } setvbuf(f, NULL, _IOFBF, 1 << 22); return f; }

static void vput(FILE *f, uint64_t x, uint64_t *bytes) { while (x >= 128) { fputc((int)(x & 127) | 128, f); x >>= 7; (*bytes)++; } fputc((int)x, f); (*bytes)++; }
static int vget(FILE *f, uint64_t *x) {   // 1 ok, 0 clean end before the first byte
  uint64_t v = 0; int sh = 0, c;
  for (;;) {
    c = getc(f);
    if (c == EOF) { if (ferror(f)) die("read error"); if (sh == 0) return 0; die("truncated varint"); }
    if (sh == 63 && c > 1) die("varint overflow");
    v |= (uint64_t)(c & 127) << sh;
    if (c < 128) break;
    sh += 7; if (sh > 63) die("bad varint");
  }
  *x = v; return 1;
}
static uint64_t vneed(FILE *f) { uint64_t x; if (!vget(f, &x)) die("truncated record"); return x; }
static int bget(FILE *f) { int c = getc(f); if (c == EOF) die("truncated record"); return c; }

// The only way lg reads a text line: getline, then refuse an embedded NUL (C string code would stop at it and
// silently drop the rest). Returns the length including any newline; -1 at end.
static ssize_t gline(char **s, size_t *cap, FILE *f) {
  ssize_t n = getline(s, cap, f);
  if (n < 0 && ferror(f)) die("read error");
  if (n > 0 && memchr(*s, 0, (size_t)n)) die("NUL byte in a text line");
  if (n > 0 && memchr(*s, '\r', (size_t)n)) die("carriage return in a text line");   // every reader, one rule
  return n;
}
static uint64_t mix64(uint64_t z) {   // splitmix64 finaliser
  z += 0x9E3779B97F4A7C15ULL; z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL; z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL; return z ^ (z >> 31);
}
// Order-dependent fingerprint of the edge stream: two independent chained 64-bit lanes. A second pass must equal the
// first edge for edge, in the same order.
static void fp_add(uint64_t h[2], uint64_t a, uint64_t b) { uint64_t e = (a << 32) | b; h[0] = mix64(h[0] ^ e); h[1] = mix64(h[1] + mix64(e ^ 0xD6E8FEB86659FD93ULL)); }
// One edge line, exactly "digits<TAB>digits<LF>" (the last line may lack the LF). 1 = an edge, 0 = clean end.
// Anything else stops: a sign, a space, an empty field, a third field, or a number past 10^11.
static int edge_line(FILE *f, uint64_t *a, uint64_t *b) {
  int c = getc_unlocked(f);
  if (c == EOF) { if (ferror(f)) die("read error in the edge stream"); return 0; }
  for (int k = 0; k < 2; k++) {
    uint64_t v = 0; int digits = 0;
    while (c >= '0' && c <= '9') { if (v > 100000000000ULL) die("number too large in the edge stream"); v = v * 10 + (uint64_t)(c - '0'); digits++; c = getc_unlocked(f); }
    if (!digits) die("malformed edge line");
    if (k == 0) { if (c != '\t') die("malformed edge line"); *a = v; c = getc_unlocked(f); }
    else {
      if (c != '\n' && c != EOF) die("malformed edge line");
      if (c == EOF && ferror(f)) die("read error in the edge stream");   // an error is never the line's end
      *b = v;
    }
  }
  return 1;
}

static uint32_t *load_u32(const char *path, uint64_t *n) {
  FILE *f = xopen(path, "rb"); fseeko(f, 0, SEEK_END); off_t sz = ftello(f); fseeko(f, 0, SEEK_SET);
  if (sz % 4) die("map file size is not a multiple of 4");
  *n = (uint64_t)sz / 4; uint32_t *a = xmalloc((size_t)sz);
  if (*n && fread(a, 4, *n, f) != *n) die("short read on map");
  fclose(f); return a;
}
static void save_u32(const char *path, const uint32_t *a, uint64_t n) {
  FILE *f = xopen(path, "wb"); if (n && fwrite(a, 4, n, f) != n) die("short write on map"); if (fclose(f)) die("close failed");
}

// ---------- names ----------
// Names in byte order, whatever order the release lists them in (Common Crawl's 2018 releases sort "ac.2-yr" before
// "ac.2"; the 2026 ones use plain byte order). All names are held in one buffer, sorted, checked unique, then written
// sorted, with ORDER = for each sorted position, the release's own id at that position (u32).
static const char *g_names; static const uint64_t *g_noff;
static int cmp_name(const void *a, const void *b) {
  return strcmp(g_names + g_noff[*(const uint32_t *)a], g_names + g_noff[*(const uint32_t *)b]);
}
static int cmd_names(const char *out, const char *posp) {
  char *line = NULL; size_t cap = 0; ssize_t len; uint64_t want = 0;
  size_t bcap = 1 << 24, blen = 0; char *buf = xmalloc(bcap);
  uint64_t ocap = 1 << 20; uint64_t *noff = xmalloc(ocap * 8);
  while ((len = gline(&line, &cap, stdin)) > 0) {
    char *t1 = strchr(line, '\t'); if (!t1) die("vertex line without a tab");
    *t1 = 0; uint64_t id = num(line, UINT32_MAX - 1, "vertex id");
    if (id != want) { fprintf(stderr, "lg: vertex id %llu where %llu expected\n", (unsigned long long)id, (unsigned long long)want); exit(3); }
    char *name = t1 + 1; size_t nl = strcspn(name, "\t\n");
    // what follows the name must be nothing, or one tab and digits (Common Crawl's host count), then the newline
    { char *rest = name + nl; if (*rest == '\t') { rest++; if (*rest < '0' || *rest > '9') die("bad vertex line"); while (*rest >= '0' && *rest <= '9') rest++; }
      if (*rest == '\n') rest++;
      if (*rest) die("bad vertex line"); }
    name[nl] = 0;
    if (!nl) die("empty vertex name");
    if (blen + nl + 1 > bcap) { while (blen + nl + 1 > bcap) bcap *= 2; buf = xrealloc(buf, bcap); }
    if (want == ocap) { ocap *= 2; noff = xrealloc(noff, ocap * 8); }
    noff[want] = blen; memcpy(buf + blen, name, nl + 1); blen += nl + 1; want++;
  }
  uint32_t *ord = xmalloc((want ? want : 1) * 4);
  for (uint64_t i = 0; i < want; i++) ord[i] = (uint32_t)i;
  g_names = buf; g_noff = noff;
  qsort(ord, want, 4, cmp_name);
  uint32_t *pos = xmalloc((want ? want : 1) * 4); uint64_t distinct = 0, dups = 0;
  FILE *o = xopen(out, "wb");
  for (uint64_t i = 0; i < want; i++) {
    const char *nm = buf + noff[ord[i]];
    if (i > 0 && strcmp(buf + noff[ord[i - 1]], nm) == 0) { dups++; pos[ord[i]] = (uint32_t)(distinct - 1); continue; }
    fputs(nm, o); fputc('\n', o); pos[ord[i]] = (uint32_t)distinct; distinct++;
  }
  if (fclose(o)) die("close failed");
  if (dups > 1000 + want / 1000) { fprintf(stderr, "lg: %llu duplicate names: a broken vertex file\n", (unsigned long long)dups); exit(3); }
  save_u32(posp, pos, want);
  free(buf); free(noff); free(ord); free(pos);
  printf("{\"names\": %llu, \"distinct\": %llu, \"duplicates_merged\": %llu}\n", (unsigned long long)want, (unsigned long long)distinct, (unsigned long long)dups);
  return 0;
}

// ---------- dictmerge ----------
typedef struct { FILE *f; char *s; size_t cap; ssize_t len; uint64_t n; } Lines;
static int lnext(Lines *l) {
  if (!l->f) return 0;
  l->len = gline(&l->s, &l->cap, l->f);
  if (l->len <= 0) return 0;
  if (l->s[l->len - 1] == '\n') l->s[--l->len] = 0;
  return 1;
}
static int cmd_dictmerge(const char *oldp, const char *relp, const char *newp, const char *mapoldp, const char *maprelp) {
  Lines a = {0}, b = {0};
  if (strcmp(oldp, "-")) a.f = xopen(oldp, "rb");
  b.f = xopen(relp, "rb");
  char idxp[4096]; snprintf(idxp, sizeof idxp, "%s.idx", newp);
  FILE *o = xopen(newp, "wb"), *ix = xopen(idxp, "wb");
  size_t mocap = 1 << 20, mrcap = 1 << 20; uint32_t *mo = xmalloc(mocap * 4), *mr = xmalloc(mrcap * 4);
  uint64_t no = 0, nr = 0, nn = 0, off = 0;
  char *last = NULL; size_t lcap = 0;
  int ha = lnext(&a), hb = lnext(&b);
  while (ha || hb) {
    int c = !ha ? 1 : !hb ? -1 : strcmp(a.s, b.s);
    const char *s = c <= 0 ? a.s : b.s; size_t sl = (size_t)(c <= 0 ? a.len : b.len);
    if (last && strcmp(last, s) >= 0) die("inputs to dictmerge are not strictly sorted");
    if (sl + 1 > lcap) { lcap = sl + 64; last = xrealloc(last, lcap); } memcpy(last, s, sl + 1);
    if (nn > UINT32_MAX - 1) die("more than 2^32 domains");
    if (nn % 4096 == 0) fprintf(ix, "%llu\t%llu\t%s\n", (unsigned long long)(nn / 4096), (unsigned long long)off, s);
    fwrite(s, 1, sl, o); fputc('\n', o); off += sl + 1;
    if (c <= 0) { if (no == mocap) { mocap *= 2; mo = xrealloc(mo, mocap * 4); } mo[no++] = (uint32_t)nn; ha = lnext(&a); }
    if (c >= 0) { if (nr == mrcap) { mrcap *= 2; mr = xrealloc(mr, mrcap * 4); } mr[nr++] = (uint32_t)nn; hb = lnext(&b); }
    nn++;
  }
  if (fclose(o) || fclose(ix)) die("close failed");
  save_u32(mapoldp, mo, no); save_u32(maprelp, mr, nr);
  printf("{\"old\": %llu, \"release\": %llu, \"union\": %llu, \"new_names\": %llu}\n",
         (unsigned long long)no, (unsigned long long)nr, (unsigned long long)nn, (unsigned long long)(nn - no));
  return 0;
}

// ---------- inbound ----------
static int cmp_u32(const void *a, const void *b) { uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b; return x < y ? -1 : x > y; }
static int cmd_inbound(uint64_t N, uint64_t E, const char *maprelp, const char *posp, const char *outp, const char *cmd, uint64_t part) {
  uint64_t M, np; uint32_t *bypos = load_u32(maprelp, &M), *pos = load_u32(posp, &np);
  if (np != N) die("release position file size differs from the node count");
  if (M > N || (N && !M)) die("release map size is impossible for the node count");
  for (uint64_t p = 1; p < M; p++) if (bypos[p] <= bypos[p - 1]) die("release map not ascending");
  { uint8_t *used = xcalloc(M ? M : 1, 1);   // POS must land every release id on a position, and use every position
    for (uint64_t i = 0; i < N; i++) { if (pos[i] >= M) die("release position out of range"); used[pos[i]] = 1; }
    for (uint64_t p = 0; p < M; p++) if (!used[p]) die("a release position is never used");
    free(used); }
  uint32_t *indeg = xcalloc(M ? M : 1, 4); uint64_t a, b, e1 = 0, prev = 0, h1[2] = {1, 2};
  FILE *p = popen(cmd, "r"); if (!p) die("cannot run the edge command");
  while (edge_line(p, &a, &b)) {
    if (a >= N || b >= N) die("edge id out of range");
    if (a < prev) die("edges are not sorted by source");
    prev = a; indeg[pos[b]]++; e1++; fp_add(h1, a, b);
  }
  if (pclose(p)) die("edge command failed on pass 1");
  if (e1 != E) { fprintf(stderr, "lg: pass 1 read %llu edges, properties say %llu\n", (unsigned long long)e1, (unsigned long long)E); exit(4); }
  uint64_t *off = xmalloc((M + 1) * 8), maxdeg = 0; off[0] = 0;   // off[t]: where target t's links start
  for (uint64_t i = 0; i < M; i++) { off[i + 1] = off[i] + indeg[i]; if (indeg[i] > maxdeg) maxdeg = indeg[i]; }
  free(indeg);
  if (off[M] != E) die("inbound offsets do not add up");
  if (part == 0 || part > E) part = E;
  if (part < maxdeg) {                                      // one target's links are never split
    fprintf(stderr, "lg: one domain has %llu links, more than a part of %llu: that part holds %llu\n",
            (unsigned long long)maxdeg, (unsigned long long)part, (unsigned long long)maxdeg);
    part = maxdeg;
  }
  uint32_t *src = xmalloc((part ? part : 1) * 4);
  FILE *o = xopen(outp, "wb"); uint64_t bytes = 0, targets = 0, links = 0, collapsed = 0, parts = 0;
  for (uint64_t t0 = 0; t0 < M;) {                          // parts in target order, so OUT stays in global order
    uint64_t base = off[t0], t1 = t0;
    while (t1 < M && off[t1 + 1] - base <= part) t1++;
    if (t1 == t0) die("a target does not fit a part");     // cannot happen: part >= maxdeg
    uint64_t pend = off[t1], e2 = 0, placed = 0, h2[2] = {1, 2}; prev = 0; parts++;
    p = popen(cmd, "r"); if (!p) die("cannot run the edge command");
    while (edge_line(p, &a, &b)) {
      if (a >= N || b >= N) die("edge id out of range");
      if (a < prev) die("edges are not sorted by source (placing pass)");
      prev = a; e2++; fp_add(h2, a, b);
      uint32_t t = pos[b];
      if (t < t0 || t >= t1) continue;
      if (off[t] >= pend) die("a placing pass differs from pass 1");
      src[off[t]++ - base] = bypos[pos[a]]; placed++;      // off[t] walks from t's start to its end
    }
    if (pclose(p)) die("edge command failed on a placing pass");
    if (e2 != E) die("a placing pass read a different number of edges");
    if (h1[0] != h2[0] || h1[1] != h2[1]) die("a placing pass read different edges than pass 1 (fingerprint)");
    if (placed != pend - base) die("a placing pass placed a different number of links than pass 1 counted");
    uint64_t s = base;                                      // after the pass off[t] is t's end, so t+1 starts there
    for (uint64_t t = t0; t < t1; s = off[t], t++) {
      uint64_t e = off[t]; if (e < s || e > pend) die("inbound offsets do not add up");
      if (s == e) continue;
      uint32_t *v = src + (s - base); uint64_t n = e - s;
      qsort(v, n, 4, cmp_u32);
      uint64_t w = 0;                                       // a link repeated through a shared name is kept once
      for (uint64_t k = 0; k < n; k++) { if (k > 0 && v[k] == v[w - 1]) { collapsed++; continue; } v[w++] = v[k]; }
      targets++; links += w; vput(o, bypos[t], &bytes); vput(o, w, &bytes);
      uint64_t ps = 0;
      for (uint64_t k = 0; k < w; k++) { uint64_t g = v[k]; vput(o, g - ps, &bytes); ps = g; }
    }
    if (s != pend) die("inbound offsets do not add up");
    t0 = t1;
  }
  if (fclose(o)) die("close failed");
  free(src); free(off);
  if (collapsed > E / 1000 + 1000) { fprintf(stderr, "lg: %llu repeated links: the release repeats edges\n", (unsigned long long)collapsed); exit(3); }
  printf("{\"nodes\": %llu, \"distinct\": %llu, \"edges\": %llu, \"targets\": %llu, \"links\": %llu, \"repeats_collapsed\": %llu, \"bytes\": %llu, \"parts\": %llu, \"part_links\": %llu, \"max_links_one_domain\": %llu}\n",
         (unsigned long long)N, (unsigned long long)M, (unsigned long long)E, (unsigned long long)targets, (unsigned long long)links, (unsigned long long)collapsed, (unsigned long long)bytes, (unsigned long long)parts, (unsigned long long)part, (unsigned long long)maxdeg);
  return 0;
}

// ---------- history reading ----------
typedef struct { uint64_t tgt, n, cap; uint32_t *src; uint8_t *first, *last; } Rec;
static void rec_room(Rec *r, uint64_t n) {
  if (n > r->cap) { r->cap = n + n / 2 + 16; r->src = xrealloc(r->src, r->cap * 4); r->first = xrealloc(r->first, r->cap); r->last = xrealloc(r->last, r->cap); }
}
typedef struct { uint64_t *s, *o, *t, n; } Blocks;   // the index's block lines: shard, offset, first target
typedef struct { FILE *f; uint64_t left, shards, used; const uint64_t *want, *wantb; uint64_t nwant; const Blocks *blk; uint64_t bi; } HistIn;   // records left in the current shard; shards opened; bytes read in it; expected records and bytes per shard
static int hgetc(HistIn *h) { int c = getc(h->f); if (c == EOF) die("truncated record"); h->used++; return c; }
static uint64_t hvar(HistIn *h) {
  uint64_t v = 0; int sh = 0, c;
  for (;;) { c = hgetc(h); if (sh == 63 && c > 1) die("varint overflow"); v |= (uint64_t)(c & 127) << sh; if (c < 128) return v; sh += 7; if (sh > 63) die("bad varint"); }
}
static void hist_shard_done(HistIn *h) {   // the shard just finished must have exactly the bytes the index records
  if (h->wantb && h->shards > 0 && h->used != h->wantb[h->shards - 1]) die("a history shard's size differs from the index");
  // and every block line the index gave for that shard must have been met in the data
  if (h->blk && h->shards > 0 && h->bi < h->blk->n && h->blk->s[h->bi] == h->shards - 1) die("a history index block was never met in its shard");
}
static int hist_next(HistIn *h, Rec *r) {
  if (!h->f) return 0;
  while (h->left == 0) {
    char m[4]; size_t k = fread(m, 1, 4, h->f);
    if (k < 4 && ferror(h->f)) die("read error in the history");
    hist_shard_done(h);
    if (k == 0) return 0;
    if (k != 4 || memcmp(m, "LGH1", 4)) die("history shard header missing");
    uint64_t c; if (fread(&c, 8, 1, h->f) != 1) die("history shard header truncated");
    if (h->want) { if (h->shards >= h->nwant || c != h->want[h->shards]) die("a history shard's record count differs from the index"); }
    h->left = c; h->shards++; h->used = 12;
  }
  uint64_t at = h->used, sh = h->shards - 1;
  r->tgt = hvar(h); if (r->tgt > UINT32_MAX) die("bad target id in history"); r->n = hvar(h); rec_room(r, r->n);
  if (h->blk && h->bi < h->blk->n && h->blk->s[h->bi] == sh && h->blk->o[h->bi] <= at) {
    // the index promised a record starting exactly here with exactly this target: the data must agree
    if (h->blk->o[h->bi] != at || h->blk->t[h->bi] != r->tgt) die("a history index block disagrees with the shard's data");
    h->bi++;
  }
  uint64_t ps = 0;
  for (uint64_t k = 0; k < r->n; k++) {
    uint64_t d = hvar(h); if (d > UINT32_MAX - ps || (k > 0 && d == 0)) die("bad source id in history");
    ps += d; r->src[k] = (uint32_t)ps; r->first[k] = (uint8_t)hgetc(h); r->last[k] = (uint8_t)hgetc(h);
    if (r->first[k] > r->last[k]) die("a link's first release is after its last");
  }
  h->left--; return 1;
}

// ---------- history writing ----------
typedef struct { const char *dir, *on_shard; uint64_t limit, bytes, recs, block_at, shard, total_bytes; FILE *f, *idx; off_t hdr; char path[4096]; uint64_t *srec, *sbytes, scap; } HistOut;
static void hist_close(HistOut *w) {
  if (!w->f) return;
  fseeko(w->f, w->hdr + 4, SEEK_SET); fwrite(&w->recs, 8, 1, w->f);
  if (fclose(w->f)) die("close failed");
  w->f = NULL;
  if (w->shard >= w->scap) { w->scap = w->scap ? w->scap * 2 : 64; w->srec = xrealloc(w->srec, w->scap * 8); w->sbytes = xrealloc(w->sbytes, w->scap * 8); }
  w->srec[w->shard] = w->recs; w->sbytes[w->shard] = w->bytes; w->shard++;
  if (w->on_shard) {
    char q[8300]; size_t o = 0; q[o++] = '\'';   // single-quote the path, escaping any quote inside it
    for (const char *s = w->path; *s; s++) { if (o + 6 >= sizeof q) die("path too long"); if (*s == '\'') { memcpy(q + o, "'\\''", 4); o += 4; } else q[o++] = *s; }
    q[o++] = '\''; q[o] = 0;
    char c[8600]; snprintf(c, sizeof c, "%s %s", w->on_shard, q);
    if (system(c) != 0) die("the on-shard command failed");
  }
}
static void hist_put(HistOut *w, const Rec *r) {
  if (w->f && w->bytes >= w->limit) hist_close(w);
  if (!w->f) {
    snprintf(w->path, sizeof w->path, "%s/h-%05llu.bin", w->dir, (unsigned long long)w->shard);
    w->f = xopen(w->path, "wb"); w->hdr = 0; fwrite("LGH1", 1, 4, w->f); uint64_t z = 0; fwrite(&z, 8, 1, w->f);
    w->bytes = 12; w->recs = 0; w->block_at = 0;
    fprintf(w->idx, "%llu\t%llu\t%llu\n", (unsigned long long)w->shard, (unsigned long long)w->bytes, (unsigned long long)r->tgt);
    w->block_at = w->bytes;
  } else if (w->bytes - w->block_at >= (1u << 20)) {
    fprintf(w->idx, "%llu\t%llu\t%llu\n", (unsigned long long)w->shard, (unsigned long long)w->bytes, (unsigned long long)r->tgt);
    w->block_at = w->bytes;
  }
  uint64_t before = w->bytes;
  vput(w->f, r->tgt, &w->bytes); vput(w->f, r->n, &w->bytes);
  uint64_t ps = 0;
  for (uint64_t k = 0; k < r->n; k++) { vput(w->f, r->src[k] - ps, &w->bytes); ps = r->src[k]; fputc(r->first[k], w->f); fputc(r->last[k], w->f); w->bytes += 2; }
  w->recs++; w->total_bytes += w->bytes - before;
}

// Reads a history index: the release it was built through and how many distinct shards it lists.
static void read_idx_head(const char *p, int *release, uint64_t *shards, uint64_t **recs, uint64_t **bytes, Blocks *blk) {
  FILE *f = xopen(p, "rb"); char *line = NULL; size_t lcap = 0; uint64_t sh, last = UINT64_MAX, nrec = 0, v[3]; *shards = 0;
  *recs = xmalloc(8 * 64); *bytes = xmalloc(8 * 64); uint64_t cap = 64;
  if (gline(&line, &lcap, f) <= 0 || strncmp(line, "#release\t", 9)) die("history index has no #release line");
  { size_t L = strlen(line); if (L && line[L - 1] == '\n') line[L - 1] = 0; }   /* only the newline: a \r or anything else fails num() */ *release = (int)num(line + 9, 254, "the index's release");
  const uint64_t BIG = 1ULL << 48;
  uint64_t *bs = NULL, *bo = NULL, *bt = NULL, nb = 0, bcap = 0;   // every block line, checked against the rest at the end
  while (gline(&line, &lcap, f) > 0) {
    if (!strncmp(line, "#shard\t", 7)) {
      if (fields(line + 7, v, 3, BIG, "an index #shard field") != 3 || v[0] != nrec) die("bad #shard line in the history index");
      uint64_t rc = v[1], by = v[2];
      if (nrec == cap) { cap *= 2; *recs = xrealloc(*recs, cap * 8); *bytes = xrealloc(*bytes, cap * 8); }
      if (rc == 0 || by <= 12) die("history index: a shard with no records");
      (*recs)[nrec] = rc; (*bytes)[nrec] = by; nrec++; continue;
    }
    if (fields(line, v, 3, BIG, "an index field") != 3) die("bad history index line");
    sh = v[0];
    if (sh != last) { if ((last == UINT64_MAX && sh != 0) || (last != UINT64_MAX && sh != last + 1)) die("history index shards not consecutive from 0"); (*shards)++; last = sh; }
    if (nb == bcap) { bcap = bcap ? bcap * 2 : 1024; bs = xrealloc(bs, bcap * 8); bo = xrealloc(bo, bcap * 8); bt = xrealloc(bt, bcap * 8); }
    bs[nb] = v[0]; bo[nb] = v[1]; bt[nb] = v[2]; nb++;
  }
  fclose(f);
  if (nrec != *shards) die("history index lacks a #shard line for every shard");
  for (uint64_t i = 0; i < nb; i++) {   // the block lines must agree with each other and with the #shard lines
    int first_of_shard = (i == 0 || bs[i] != bs[i - 1]);
    if (first_of_shard && bo[i] != 12) die("history index: a shard's first block is not at its start");
    if (!first_of_shard && bo[i] <= bo[i - 1]) die("history index: block offsets not increasing");
    if (bo[i] >= (*bytes)[bs[i]]) die("history index: block offset past its shard's size");
    if (i > 0 && bt[i] <= bt[i - 1]) die("history index: first targets not increasing");
  }
  if (blk) { blk->s = bs; blk->o = bo; blk->t = bt; blk->n = nb; } else { free(bs); free(bo); free(bt); }
}
static int cmd_merge(int ri, const char *prevcmd, const char *previdx, const char *mapoldp, const char *relp, const char *outdir, uint64_t shard_kb, const char *on_shard) {
  if (ri < 0 || ri > 254) die("release index must be 0..254");
  uint64_t nmo = 0, want_shards = 0, *want_recs = NULL, *want_bytes = NULL; uint32_t *mo = NULL; int prev_release = -1;
  HistIn h = {0};
  if (!strcmp(prevcmd, "-") != !strcmp(previdx, "-")) die("give both the history command and its index, or neither");
  static Blocks pblk;
  if (strcmp(previdx, "-")) { read_idx_head(previdx, &prev_release, &want_shards, &want_recs, &want_bytes, &pblk); h.want = want_recs; h.wantb = want_bytes; h.nwant = want_shards; h.blk = &pblk; }
  if (ri != prev_release + 1) { fprintf(stderr, "lg: release %d is not the one after %d\n", ri, prev_release); exit(3); }
  if (strcmp(prevcmd, "-")) { mo = load_u32(mapoldp, &nmo); h.f = popen(prevcmd, "r"); if (!h.f) die("cannot run the history command"); }
  FILE *rf = xopen(relp, "rb");
  char ip[4096]; snprintf(ip, sizeof ip, "%s/history.idx", outdir);
  HistOut w = { .dir = outdir, .on_shard = on_shard, .limit = shard_kb << 10 }; w.idx = xopen(ip, "wb");
  fprintf(w.idx, "#release\t%d\n", ri);
  Rec hp = {0}, rr = {0}, out = {0};
  uint64_t links = 0, fresh = 0, again = 0, carried = 0, targets = 0, prev_t = 0; int have_prev_t = 0;
  int hh = hist_next(&h, &hp);
  uint64_t rt = 0, rn = 0; int hr = vget(rf, &rt);
  if (hr) { rn = vneed(rf); if (rt > UINT32_MAX) die("bad target id in release"); }
  #define REMAP_PREV() do { if (hp.tgt >= nmo) die("history id outside the old map"); hp.tgt = mo[hp.tgt]; \
      for (uint64_t k = 0; k < hp.n; k++) { if (hp.src[k] >= nmo) die("history id outside the old map"); hp.src[k] = mo[hp.src[k]]; } } while (0)
  if (hh) REMAP_PREV();
  while (hh || hr) {
    int take_h = hh && (!hr || hp.tgt <= rt), take_r = hr && (!hh || rt <= hp.tgt);
    out.tgt = take_h ? hp.tgt : rt; out.n = 0;
    if (have_prev_t && out.tgt <= prev_t) die("targets not ascending");
    prev_t = out.tgt; have_prev_t = 1;
    rec_room(&rr, take_r ? rn : 0); rr.n = 0;
    if (take_r) { uint64_t ps = 0; for (uint64_t k = 0; k < rn; k++) { uint64_t d = vneed(rf); if (d > UINT32_MAX - ps || (k > 0 && d == 0)) die("bad source id in release"); ps += d; rr.src[k] = (uint32_t)ps; } rr.n = rn; }
    uint64_t i = 0, j = 0, hn = take_h ? hp.n : 0;
    rec_room(&out, hn + rr.n);
    while (i < hn || j < rr.n) {
      if (i < hn && hp.last[i] >= ri) die("release index not after the history's last release");
      if (j >= rr.n || (i < hn && hp.src[i] < rr.src[j])) { out.src[out.n] = hp.src[i]; out.first[out.n] = hp.first[i]; out.last[out.n] = hp.last[i]; carried++; i++; }
      else if (i >= hn || rr.src[j] < hp.src[i]) { out.src[out.n] = rr.src[j]; out.first[out.n] = (uint8_t)ri; out.last[out.n] = (uint8_t)ri; fresh++; j++; }
      else { out.src[out.n] = hp.src[i]; out.first[out.n] = hp.first[i]; out.last[out.n] = (uint8_t)ri; again++; i++; j++; }
      if (out.n > 0 && out.src[out.n] <= out.src[out.n - 1]) die("merged sources not ascending");
      out.n++;
    }
    hist_put(&w, &out); targets++; links += out.n;
    if (take_h) { hh = hist_next(&h, &hp); if (hh) REMAP_PREV(); }
    if (take_r) { hr = vget(rf, &rt); if (hr) { rn = vneed(rf); if (rt > UINT32_MAX) die("bad target id in release"); } }
  }
  if (h.f && h.left == 0) { Rec tail = {0}; if (hist_next(&h, &tail)) die("previous history has records past its index"); }
  if (h.f && (h.left != 0 || h.shards != want_shards)) {
    fprintf(stderr, "lg: previous history incomplete: %llu of %llu shards, %llu records missing in the last\n",
            (unsigned long long)h.shards, (unsigned long long)want_shards, (unsigned long long)h.left);
    exit(4);
  }
  if (h.f && getc(h.f) != EOF) die("previous history has data after its last shard");
  if (h.f && pclose(h.f)) die("history command failed");
  hist_close(&w);
  for (uint64_t k = 0; k < w.shard; k++) fprintf(w.idx, "#shard\t%llu\t%llu\t%llu\n", (unsigned long long)k, (unsigned long long)w.srec[k], (unsigned long long)w.sbytes[k]);
  fclose(rf); if (fclose(w.idx)) die("close failed");
  printf("{\"release_index\": %d, \"targets\": %llu, \"links\": %llu, \"new\": %llu, \"seen_again\": %llu, \"not_seen_this_release\": %llu, \"shards\": %llu, \"bytes\": %llu}\n",
         ri, (unsigned long long)targets, (unsigned long long)links, (unsigned long long)fresh, (unsigned long long)again,
         (unsigned long long)carried, (unsigned long long)w.shard, (unsigned long long)w.total_bytes);
  return 0;
}

// ---------- readers ----------
static int rec_read(FILE *f, Rec *r) {   // one record without a shard header; 0 at clean end of file
  uint64_t t; if (!vget(f, &t)) return 0;
  if (t > UINT32_MAX) die("bad target id in history");
  r->tgt = t; r->n = vneed(f); rec_room(r, r->n); uint64_t ps = 0;
  for (uint64_t k = 0; k < r->n; k++) {
    uint64_t d = vneed(f); if (d > UINT32_MAX - ps || (k > 0 && d == 0)) die("bad source id in history");
    ps += d; r->src[k] = (uint32_t)ps; r->first[k] = (uint8_t)bget(f); r->last[k] = (uint8_t)bget(f);
    if (r->first[k] > r->last[k]) die("a link's first release is after its last");
  }
  return 1;
}
typedef struct { char **s; uint64_t n; } Names;
static Names load_names(const char *p) {
  FILE *f = xopen(p, "rb"); Names r = {0}; uint64_t cap = 1024; r.s = xmalloc(cap * sizeof(char *));
  char *line = NULL; size_t c = 0; ssize_t l;
  while ((l = gline(&line, &c, f)) > 0) { if (line[l - 1] == '\n') line[--l] = 0; if (r.n == cap) { cap *= 2; r.s = xrealloc(r.s, cap * sizeof(char *)); } r.s[r.n++] = strdup(line); }
  fclose(f); free(line); return r;
}
static int cmd_dump(const char *dir, const char *namesp) {
  Names nm = load_names(namesp); char ip[4096]; snprintf(ip, sizeof ip, "%s/history.idx", dir);
  FILE *ix = xopen(ip, "rb"); uint64_t sh, offv, ft, lastsh = UINT64_MAX; Rec r = {0}; char *ln = NULL; size_t lncap = 0; ssize_t lnl; (void)offv; (void)ft;
  while ((lnl = gline(&ln, &lncap, ix)) > 0) {
    if (ln[0] == '#') continue;
    { uint64_t v[3]; if (fields(ln, v, 3, 1ULL << 48, "an index field") != 3) die("bad history index line"); sh = v[0]; offv = v[1]; ft = v[2]; }
    if (sh == lastsh) continue;
    lastsh = sh;
    char p[4096]; snprintf(p, sizeof p, "%s/h-%05llu.bin", dir, (unsigned long long)sh);
    HistIn h = { .f = xopen(p, "rb") };
    while (hist_next(&h, &r)) for (uint64_t k = 0; k < r.n; k++) {
      if (r.tgt >= nm.n || r.src[k] >= nm.n) die("id outside the names list");
      printf("%s\t%s\t%u\t%u\n", nm.s[r.tgt], nm.s[r.src[k]], r.first[k], r.last[k]);
    }
    fclose(h.f);
  }
  fclose(ix); return 0;
}
static void rev_domain(const char *d, char *out, size_t cap) {   // example.co.uk -> uk.co.example
  size_t n = strlen(d), o = 0; ssize_t end = (ssize_t)n;
  for (ssize_t i = (ssize_t)n - 1; i >= -1; i--) {
    if (i == -1 || d[i] == '.') { size_t len = (size_t)(end - i - 1); if (o + len + 2 > cap) die("domain too long"); if (o) out[o++] = '.'; memcpy(out + o, d + i + 1, len); o += len; end = i; }
  }
  out[o] = 0;
}
static int cmd_lookup(const char *dir, const char *namesp, const char *domain) {
  Names nm = load_names(namesp); char rv[1024]; rev_domain(domain, rv, sizeof rv);
  uint64_t lo = 0, hi = nm.n; while (lo < hi) { uint64_t m = (lo + hi) / 2; if (strcmp(nm.s[m], rv) < 0) lo = m + 1; else hi = m; }
  if (lo >= nm.n || strcmp(nm.s[lo], rv)) { printf("{\"domain\": \"%s\", \"known\": false}\n", domain); return 0; }
  uint64_t id = lo; char ip[4096]; snprintf(ip, sizeof ip, "%s/history.idx", dir);
  FILE *ix = xopen(ip, "rb"); uint64_t sh, offv, ft, bsh = UINT64_MAX, boff = 0; char *ln = NULL; size_t lncap = 0; ssize_t lnl;
  while ((lnl = gline(&ln, &lncap, ix)) > 0) {
    if (ln[0] == '#') continue;
    { uint64_t v[3]; if (fields(ln, v, 3, 1ULL << 48, "an index field") != 3) die("bad history index line"); sh = v[0]; offv = v[1]; ft = v[2]; }
    if (ft > id) break;
    bsh = sh; boff = offv;
  }
  fclose(ix);
  if (bsh == UINT64_MAX) { printf("{\"domain\": \"%s\", \"known\": true, \"linking_domains\": 0}\n", domain); return 0; }
  char p[4096]; snprintf(p, sizeof p, "%s/h-%05llu.bin", dir, (unsigned long long)bsh);
  FILE *f = xopen(p, "rb"); fseeko(f, (off_t)boff, SEEK_SET); Rec r = {0};
  while (rec_read(f, &r)) {
    if (r.tgt > id) break;
    if (r.tgt == id) {
      printf("{\"domain\": \"%s\", \"known\": true, \"linking_domains\": %llu}\n", domain, (unsigned long long)r.n);
      for (uint64_t k = 0; k < r.n; k++) printf("%s\t%u\t%u\n", nm.s[r.src[k]], r.first[k], r.last[k]);
      fclose(f); return 0;
    }
  }
  fclose(f); printf("{\"domain\": \"%s\", \"known\": true, \"linking_domains\": 0}\n", domain); return 0;
}

int main(int argc, char **argv) {
  if (argc == 4 && !strcmp(argv[1], "names")) return cmd_names(argv[2], argv[3]);
  if (argc == 7 && !strcmp(argv[1], "dictmerge")) return cmd_dictmerge(argv[2], argv[3], argv[4], argv[5], argv[6]);
  if ((argc == 8 || argc == 9) && !strcmp(argv[1], "inbound")) return cmd_inbound(num(argv[2], UINT32_MAX, "N"), num(argv[3], 1ULL << 40, "E"), argv[4], argv[5], argv[6], argv[7], argc == 9 ? num(argv[8], 1ULL << 40, "PART_LINKS") : 0);
  if ((argc == 9 || argc == 10) && !strcmp(argv[1], "merge")) return cmd_merge((int)num(argv[2], 254, "REL_INDEX"), argv[3], argv[4], argv[5], argv[6], argv[7], num(argv[8], 1ULL << 24, "SHARD_KB"), argc == 10 ? argv[9] : NULL);
  if (argc == 3 && !strcmp(argv[1], "checkidx")) {   // full validation of a history index; prints its release
    int rel; uint64_t sh, *rc, *by; read_idx_head(argv[2], &rel, &sh, &rc, &by, NULL);
    printf("%d\n", rel); return 0;
  }
  if (argc == 4 && !strcmp(argv[1], "dump")) return cmd_dump(argv[2], argv[3]);
  if (argc == 5 && !strcmp(argv[1], "lookup")) return cmd_lookup(argv[2], argv[3], argv[4]);
  fprintf(stderr, "usage: see the header of lg.c\n"); return 2;
}
