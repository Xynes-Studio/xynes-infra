# Processor image stability

The first build implementation put the controller commit, Actions run and whole
source-manifest hash into every image's OCI labels. Even a frontend-only release
therefore changed the scanner and LibreOffice image digests. The host correctly
refused replacing those always-awake processors without an independent drain
contract, making subsequent application deployments impossible.

The two processor images now carry a stable component-content identity. The
approved source manifest records the exact allowlisted Git paths, executable
modes and SHA-256 file contents, plus the recipe and consumed immutable base
images. Context preparation independently checks that inventory against the
pinned Git objects and actual recipe. Unrelated commits cannot change this
identity; changed processor files, modes, recipe or bases do change it.

Fresh builds use a fixed historical `SOURCE_DATE_EPOCH` derived from that identity
and BuildKit's `rewrite-timestamp=true` and `compatibility-version=30` exporter
options. Controller/run/source-manifest provenance remains in the complete
release receipts and native attestation flow. The host still requires the exact
approved image digest and complete rendered processor specification before
retaining an existing process. Actual processor changes still refuse deployment
until a separately verified drain contract exists.

Two independent local builds with cache disabled and the dedicated builder cache
cleared between builds produced identical scanner manifests and configs. The
first LibreOffice comparison exposed seven generated installation cache files
whose contents encoded filesystem identity despite normalized file timestamps.
The recipe removes those fontconfig caches and the ldconfig auxiliary cache.
It preserves the fonts, font configuration, runtime linker cache, package
database and installed-package inventory. Fontconfig can regenerate its user
cache in the existing `/tmp/.cache` location. Two further fresh LibreOffice builds
then produced identical manifests and configs.

Exact digests, component identities, module hashes and 124 passing scoped tests
are recorded in [processor-reproducibility.json](processor-reproducibility.json).
The builds used explicit planning controller identities, distinct source contexts
and an unrelated frontend commit change. This proves local image reproducibility;
it does not establish protected workflow execution, registry publication, native
attestation, hosted readiness or release approval. Existing earlier application
archives remain bound to their original complete build inputs and cannot be
combined into a new release receipt.

BuildKit documents the timestamp and exporter behavior in its
[reproducible build documentation](https://github.com/moby/buildkit/blob/master/docs/build-repro.md).
