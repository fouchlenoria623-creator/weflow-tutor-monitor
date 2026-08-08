# Contributing

Contributions are welcome, especially parser fixtures, false-positive tests, accessibility improvements and documentation.

1. Create an issue describing the behavior and expected result.
2. Use only synthetic messages. Never submit real group names, IDs, senders, addresses, phone numbers or screenshots.
3. Add or update a focused unit test.
4. Run:

```powershell
python -m unittest discover -v -p 'test_*.py'
python -m compileall -q .
python .\tools\privacy_check.py
```

5. Keep ranking changes explainable. Document new hard filters because a false positive can hide a valid order.
