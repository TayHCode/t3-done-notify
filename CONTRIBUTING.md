# Contributing

Use Python 3.10 or newer. Install the one runtime dependency and run the tests:

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -q
```

Tests use temporary directories, mock T3 protocol traffic and local HTTP servers. They must not need a running T3
instance, real tokens, a real webhook or network access beyond the local machine. Keep host names, addresses, paths
and credentials out of code, tests and docs; use the `T3_*` and `WEBHOOK_*` settings described in the README.
