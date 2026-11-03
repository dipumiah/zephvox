# Contributing to zephvox

Thanks for helping. A few things to know first.

## License agreement

zephvox is open source (AGPL-3.0) and also sold under commercial licenses (see
[LICENSING.md](LICENSING.md)). So that both stay possible, every contributor agrees to the
[Contributor License Agreement](CLA.md). You keep the copyright to your work, and everything we
accept stays available as open source.

To sign, add this sentence to the description of your first pull request, with your name:

> I have read the zephvox Contributor License Agreement (CLA.md, version 1.0) and I agree to it.
> Name: …

Pull requests can't be merged without it.

## Setting up

```sh
git clone git@github.com:dipumiah/zephvox.git
cd zephvox
uv sync --extra models --extra cpu --extra web    # or cuda / rocm instead of cpu
uv run zephvox setup
```

## Before you open a pull request

```sh
uv run pytest                 # fast unit tests, no models needed
cd web && npm test            # if you changed the GUI; `npm run build` updates src/zephvox/web/static
```

- Keep changes focused, match the style of the code around them, and add tests for new behaviour.
- New dependencies or models: say which license they use. Only permissive (MIT, BSD, Apache) or
  LGPL ones can be accepted, so the commercial license stays possible.
