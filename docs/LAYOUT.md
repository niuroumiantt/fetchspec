"""Local archive layout (Spark is a later move, not the download target).

This environment is isolated from Spark. Binaries stay on the machine that
fetched them. Git only holds rules and code.

Default root: `$FETCHSPEC_DATA_ROOT` or `~/.local/share/fetchspec/`

    <root>/
      LAYOUT.json                 # version + Spark destination map
      blobs/<2hex>/<sha256>.ext   # immutable bytes; same shape as
                                  # inresearch acquisition/blobs/
      library/<sheet>/<company_en>/<product_line>/<model>/
                                  # same shape as inresearch product/library/
        <company_id>__<model>__<DS|PB|WEB|…>__vNA__<date>__en__<sha8>.ext
      ledger/catalog.json         # one record per unique sha256
      ledger/runs/<run_id>.json   # crawl receipts

Identity
- Bytes: sha256. Identical files are stored once in blobs/.
- Research row: company_id + product_line (inresearch data/products.json).
- Spark doc_id: unassigned locally (null). Assigned when merging catalog.json
  into data/product_library_index.json on the research host.

Kinds
- PDF → usually DS (datasheet); brief/brochure/whitepaper encoded in doc_type.
- HTML product page → WEB. HTML is a snapshot, not a spec original.

When Spark is up, move trees, do not re-crawl:

    rsync -a --partial library/ spark:.local/share/inresearch.ai/product/library/
    rsync -a --partial blobs/   spark:.local/share/inresearch.ai/acquisition/blobs/

Then merge ledger/catalog.json records into the product library index.
Do not rsync catalog.json over an existing index.
"""
