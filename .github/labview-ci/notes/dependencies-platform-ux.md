Dependency management now separates Windows and Linux into dedicated tabs, with
platform-specific status, monitoring and update approvals. Windows retains its
VIPM workflow; only Linux offers the experimental LVPM alternative. Tooling
previews never change the active worker, candidate build/verification/explicit
activation remain separate, and detailed inventories and provenance expand on
demand. Other dashboard pages identify affected platforms instead of implying
that both workers need a dependency update. Dependency banners use text-safe DOM
rendering and HTTP(S)-only review links rather than interpreting navigation text
as HTML.
