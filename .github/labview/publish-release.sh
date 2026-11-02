#!/usr/bin/env bash
# Publish the version currently in the catalog: immutable tag, moving aliases,
# rolling channel tags, GitHub Release.
#
# This used to live inline in release.yml, which meant it only ever ran when a
# push to main touched catalog.json. Pushes made with the built-in GITHUB_TOKEN
# do not trigger workflows, so every catalog bump written by promote-release.yml
# was announced in the catalog and then never tagged: 4.10.2, 4.10.3 and 4.11.11
# are all phantom versions created that way. Extracting it here lets each workflow
# publish inside its own run, where no trigger is involved.
#
# Idempotent: an existing tag is left alone and its release notes refreshed.
#
# Environment:
#   GH_TOKEN        required, for gh release create/edit
#   FORCE_ALIASES   optional, "true" re-points v<major>/v<major>.<minor> even when
#                   the tag already existed
set -euo pipefail

CAT=.github/labview-ci/catalog.json
cat_get() { python3 -c "import json,sys;print(json.load(open('$CAT')).get(sys.argv[1],'') or '')" "$1"; }

VER=$(cat_get version)
case "$VER" in
  [0-9]*.[0-9]*.[0-9]*) : ;;
  *) echo "::error::catalog version '$VER' is not MAJOR.MINOR.PATCH"; exit 1 ;;
esac

MAJOR=${VER%%.*}; REST=${VER#*.}; MINOR=${REST%%.*}
TAG="v${VER}"; MAJOR_ALIAS="v${MAJOR}"; MINOR_ALIAS="v${MAJOR}.${MINOR}"
echo "version=$VER  tag=$TAG  aliases=$MAJOR_ALIAS,$MINOR_ALIAS"

git config user.name  "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

NEW_TAG=false
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "Tag ${TAG} already exists."
else
  git tag -a "${TAG}" -m "LabVIEW CI ${TAG}"
  git push origin "${TAG}"
  NEW_TAG=true
  echo "Created tag ${TAG}."
fi

# --- alias contract -----------------------------------------------------------
#
#   v<major>.<minor>   newest patch on that line          (moves every release)
#   v<major>           newest release BLESSED as stable   (moves only on promotion)
#   dev / beta / stable  the three channels, as rolling tags
#
# v<major> used to track the tip, which meant the channel system never reached
# the people it was for: the README tells consumers to pin @v4, so every client
# following the documentation was auto-tracking dev builds. `stable` sat on
# v4.10.0 for weeks while every @v4 consumer ran v4.12.4, and diagnostic releases
# went straight to production repos.
#
# Now @v4 means what the README always promised: the latest release the owner has
# actually blessed. Consumers who want the edge pin v<major>.<minor>, or the
# rolling `dev` / `beta` tags.

# Highest v<M>.<m>.<p> tag pointing at the same commit as $1, without the leading v.
version_at_ref() {
  local sha
  sha=$(git rev-parse -q --verify "$1^{}" 2>/dev/null) || return 1
  git tag --list 'v*.*.*' --points-at "$sha" | sed 's/^v//' \
    | sort -t. -k1,1n -k2,2n -k3,3n | tail -1
}

# True when $1 is a strictly higher version than $2.
version_gt() {
  [ "$1" != "$2" ] && \
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)" = "$1" ]
}

# The minor alias still tracks the newest patch on its line.
if [ "$NEW_TAG" = "true" ] || [ "${FORCE_ALIASES:-false}" = "true" ]; then
  git tag -f "$MINOR_ALIAS" "$TAG^{}"
  git push -f origin "$MINOR_ALIAS"
  echo "Moved alias ${MINOR_ALIAS} -> ${TAG}."
fi

STABLE_VER=$(cat_get stableVersion)
if [ -z "$STABLE_VER" ]; then
  echo "No stableVersion set; leaving ${MAJOR_ALIAS} untouched."
elif [ "${STABLE_VER%%.*}" != "$MAJOR" ]; then
  echo "stableVersion ${STABLE_VER} is not in major ${MAJOR}; leaving ${MAJOR_ALIAS} untouched."
elif ! git rev-parse -q --verify "refs/tags/v${STABLE_VER}^{}" >/dev/null; then
  echo "::warning::stableVersion ${STABLE_VER} has no v${STABLE_VER} tag; leaving ${MAJOR_ALIAS} untouched."
else
  ALIAS_VER=$(version_at_ref "$MAJOR_ALIAS" || true)
  if [ -n "$ALIAS_VER" ] && [ "$ALIAS_VER" = "$STABLE_VER" ]; then
    echo "Alias ${MAJOR_ALIAS} already at v${STABLE_VER}."
  elif [ -n "$ALIAS_VER" ] && ! version_gt "$STABLE_VER" "$ALIAS_VER" \
       && [ "${FORCE_ALIASES:-false}" != "true" ]; then
    # Refusing to move the alias backwards is the whole safety property here.
    # Consumers pinned at @v<major> update automatically and without review, so a
    # backwards move is a silent mass DOWNGRADE -- and it would happen the first
    # time this ran, with the alias on 4.14.1 and stableVersion still 4.10.0.
    # The alias simply stops advancing until a promotion overtakes it.
    echo "::notice::${MAJOR_ALIAS} is at v${ALIAS_VER}; stableVersion v${STABLE_VER} is not newer, so the alias stays put (it never moves backwards). Promote a release >= v${ALIAS_VER} to advance it."
  else
    git tag -f "$MAJOR_ALIAS" "v${STABLE_VER}^{}"
    git push -f origin "$MAJOR_ALIAS"
    echo "Moved alias ${MAJOR_ALIAS} -> v${STABLE_VER} (stable)."
  fi
fi

# Rolling channel tags. `stableVersion` / `betaVersion` are set by
# promote-release.py (the owner's "Mark as stable release" button) and are absent
# until the first promotion, in which case this is a no-op. Only moves a tag when
# it is not already where it belongs.
move_channel() {
  local tier="$1" want_ver="$2"
  [ -n "$want_ver" ] || return 0
  if ! git rev-parse -q --verify "refs/tags/v${want_ver}^{}" >/dev/null; then
    echo "::warning::${tier}Version ${want_ver} has no v${want_ver} tag yet; leaving '${tier}' unchanged."
    return 0
  fi
  local want have
  want=$(git rev-parse "v${want_ver}^{}")
  have=$(git rev-parse -q --verify "refs/tags/${tier}^{}" 2>/dev/null || echo "")
  if [ "$want" != "$have" ]; then
    git tag -f "$tier" "v${want_ver}^{}"
    git push -f origin "$tier"
    echo "Moved rolling '${tier}' tag -> v${want_ver}."
  else
    echo "Rolling '${tier}' tag already at v${want_ver}."
  fi
}
move_channel stable "$(cat_get stableVersion)"
move_channel beta   "$(cat_get betaVersion)"
# `dev` is the tip by definition -- it needs no catalog pointer. It exists so the
# people who actually want every build have somewhere to pin now that v<major>
# tracks stable instead.
move_channel dev    "$VER"

# Release notes come from the catalog's newest history entry.
python3 - "$CAT" > /tmp/relnotes.md <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding='utf-8'))
rels = (c.get('history') or {}).get('releases') or []
rel = rels[0] if rels else {}
out = []
if rel.get('summary'): out += [rel['summary'], '']
if rel.get('notes'):   out += [rel['notes'], '']
for h in rel.get('highlights') or []: out.append(f'- {h}')
out += ['', 'Publication does not promote this release to beta or stable. '
        'The major alias (e.g. `@v%s`) follows stable promotions, not every publication. '
        'Pin the immutable tag `@v%s` to opt into this release explicitly.'
        % (str(c.get('version','1')).split('.')[0], c.get('version',''))]
open('/tmp/reltitle.txt','w').write(rel.get('title') or f"LabVIEW CI {c.get('version','')}")
print('\n'.join(out))
PY

if gh release view "${TAG}" >/dev/null 2>&1; then
  gh release edit "${TAG}" --title "$(cat /tmp/reltitle.txt)" --notes-file /tmp/relnotes.md
  echo "Updated release ${TAG}."
else
  gh release create "${TAG}" --target "$(git rev-parse HEAD)" \
    --title "$(cat /tmp/reltitle.txt)" --notes-file /tmp/relnotes.md
  echo "Published release ${TAG}."
fi

{
  echo "## Released ${TAG}"
  echo ""
  echo "- Immutable tag \`${TAG}\` published."
  echo "- Minor alias \`${MINOR_ALIAS}\` tracks the newest patch on its line."
  echo "- Major alias \`${MAJOR_ALIAS}\` follows stable promotions, not every publication."
  echo "- Configured stable release: \`${STABLE_VER:-not configured}\`."
  echo ""
  echo "Publication does not promote beta or stable. Pin \`@${TAG}\` to opt in explicitly."
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
