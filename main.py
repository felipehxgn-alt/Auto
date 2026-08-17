name: Robo de Midias

on:
  schedule:
    - cron: '*/5 * * * *'
  workflow_dispatch:

jobs:
  processar-midias:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run script
        env:
          DROPBOX_ACCESS_TOKEN: ${{ secrets.DROPBOX_ACCESS_TOKEN }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          PHOTOROOM_API_KEY: ${{ secrets.PHOTOROOM_API_KEY }}
        run: python main.py
