# Linux leg of tools/release-check.py (DD-v3-tests § 3.2).
# Python at the kit's 3.11 floor (AGENTS.md), pinned by digest; the image tag is
# derived from this file's sha256, so any edit here builds a new image.
# python:3.11-slim, Python 3.11.18, pulled 2026-11-04.
FROM python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893

# git: engine tests and the preflight read history; bash: test-adapter.sh;
# zsh: test_relay_monitor's shell-inertness tests run hints in zsh (early run, 2026-11-04).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git bash zsh \
    && rm -rf /var/lib/apt/lists/*

ENV ADT_TOOLCHAINS=/opt/adt-toolchains
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils gcc libc6-dev; \
    rm -rf /var/lib/apt/lists/*; \
    case "$(dpkg --print-architecture)" in \
      arm64) rust_target=aarch64-unknown-linux-gnu; node_platform=linux-arm64; ts_platform=linux-arm64; \
        rust_sha=a48fb1e853be56ce9b8e2751f5464dbd5e16440cd81e237920f3a5e7e806333e; \
        node_sha=406fe3fbde1c054adff8b297c16fdb5af6659db327c1ac8acd46b56ef1af9a92; \
        ts_native_sha='Qh4eU4/y3yDjnfjjyPYihMj5/ODIlmt+Bzu17OI+fiSRDW57QmU5SiN63exPRNJPKUzcc1INa1NXdrJ+MqHjUQ==' ;; \
      amd64) rust_target=x86_64-unknown-linux-gnu; node_platform=linux-x64; ts_platform=linux-x64; \
        rust_sha=231fcae47ee950621374725b5fc3b5441d2fbda2955ac45dd7dc816d8876cfe7; \
        node_sha=d28a96af6c38596f6d9d97c0ee531c13d238d757742387d1928de822d959e997; \
        ts_native_sha='EYdf2cNg7rgCWJnxCdJ+F3V39O8ihb37eHAu1LK8oAFizgTQbPOK7zHHXbPt8rX24COqODXeI3sIf0fCXG7H/A==' ;; \
      *) exit 1 ;; \
    esac; \
    downloads=$(mktemp -d); cd "$downloads"; \
    curl --fail --location --silent --show-error "https://static.rust-lang.org/dist/rust-1.99.1-${rust_target}.tar.xz" -o rust.tar.xz; \
    curl --fail --location --silent --show-error "https://nodejs.org/dist/v26.13.0/node-v26.13.0-${node_platform}.tar.xz" -o node.tar.xz; \
    curl --fail --location --silent --show-error 'https://registry.npmjs.org/typescript/-/typescript-7.0.2.tgz' -o typescript.tgz; \
    curl --fail --location --silent --show-error "https://registry.npmjs.org/@typescript/typescript-${ts_platform}/-/typescript-${ts_platform}-7.0.2.tgz" -o typescript-native.tgz; \
    printf '%s  rust.tar.xz\n%s  node.tar.xz\n' "$rust_sha" "$node_sha" | sha256sum --check --strict -; \
    python3 -c 'import base64,hashlib,pathlib,sys; pairs=[("typescript.tgz","8FYau96o3NKOhbjKi/qNvG/W5jhzxkbdm5sj9AbZ/5T5sWqn3hJgLfGx27sRKZWTvyzCP8dLRBTf5tBTSRVUNA=="),("typescript-native.tgz",sys.argv[1])]; all_ok=all(base64.b64encode(hashlib.sha512(pathlib.Path(name).read_bytes()).digest()).decode()==expected for name,expected in pairs); sys.exit(0 if all_ok else 1)' "$ts_native_sha"; \
    tar -xJf rust.tar.xz; \
    "rust-1.99.1-${rust_target}/install.sh" --prefix="$ADT_TOOLCHAINS/rust-1.99.1" --components="rustc,rust-std-${rust_target}" --disable-ldconfig; \
    mkdir -p "$ADT_TOOLCHAINS/node-v26.13.0" "$ADT_TOOLCHAINS/typescript-7.0.2/node_modules/typescript" "$ADT_TOOLCHAINS/typescript-7.0.2/node_modules/@typescript/typescript-${ts_platform}"; \
    tar -xJf node.tar.xz --strip-components=1 -C "$ADT_TOOLCHAINS/node-v26.13.0"; \
    tar -xzf typescript.tgz --strip-components=1 -C "$ADT_TOOLCHAINS/typescript-7.0.2/node_modules/typescript"; \
    tar -xzf typescript-native.tgz --strip-components=1 -C "$ADT_TOOLCHAINS/typescript-7.0.2/node_modules/@typescript/typescript-${ts_platform}"; \
    cd /; rm -rf "$downloads"; \
    test "$("$ADT_TOOLCHAINS/rust-1.99.1/bin/rustc" --version)" = 'rustc 1.99.1 (58cf8f9c3 2026-10-13)'; \
    test "$("$ADT_TOOLCHAINS/node-v26.13.0/bin/node" --version)" = 'v26.13.0'
