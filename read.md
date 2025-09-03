# CSV to GitHub Project Importer

Un script Python care citește un fișier **CSV** și creează task-uri în **GitHub Projects (v2)**.  
Poți alege să creezi:
- **Issues** într-un repository (și apoi să le adaugi ca item-uri în Project).
- **Draft Issues** direct în Project (fără repo).

---

## 🚀 Instalare

1. Clonează acest repo / descarcă fișierul `csv_to_github_project.py`.
2. (Opțional, recomandat) Creează și activează un mediu virtual:
   ```bash
   python3 -m venv venv
   source venv/bin/activate   # macOS/Linux
   # venv\Scripts\activate    # Windows
