# mvp zip: backend update for Brandslens-mvp2

Replace these two files in the repo (same paths), commit, and redeploy the backend:

- app/services/classifier.py   (the one the running app uses)
- classifier.py                (root copy, kept in sync)

What changed: the AI severity prompt only named Nigerian IDs (BVN, NIN) as signs of phishing.
It now also names bank logins, card details, SSN, National Insurance and SIN, and tells the
classifier to judge items in whatever language they are written in. Nothing else changed.
