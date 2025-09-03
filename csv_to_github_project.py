#!/usr/bin/env python3
import argparse
import csv
import os
import sys
import time
from typing import Dict, Any, List, Optional
import requests

GQL_ENDPOINT = "https://api.github.com/graphql"

def gql(token: str, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(
        GQL_ENDPOINT,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            # necesar ca să fie disponibile Projects v2 mutations (ex: createProjectV2DraftIssue)
            "GraphQL-Features": "projects_next_graphql",
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"GraphQL HTTP {r.status_code}: {r.text}")
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


def get_project_and_fields(token: str, owner: str, number: int) -> Dict[str, Any]:
    q_user = """
    query($owner:String!, $number:Int!) {
      user(login:$owner) {
        projectV2(number:$number) {
          id
          fields(first: 100) {
            nodes {
              __typename
              ... on ProjectV2FieldCommon { id name dataType }
              ... on ProjectV2SingleSelectField { id name dataType options { id name } }
              ... on ProjectV2IterationField { id name dataType }
            }
          }
        }
      }
    }
    """
    q_org = """
    query($owner:String!, $number:Int!) {
      organization(login:$owner) {
        projectV2(number:$number) {
          id
          fields(first: 100) {
            nodes {
              __typename
              ... on ProjectV2FieldCommon { id name dataType }
              ... on ProjectV2SingleSelectField { id name dataType options { id name } }
              ... on ProjectV2IterationField { id name dataType }
            }
          }
        }
      }
    }
    """

    # 1) încearcă user
    try:
        d = gql(token, q_user, {"owner": owner, "number": number})
        user_proj = (d.get("user") or {}).get("projectV2")
    except RuntimeError:
        user_proj = None

    proj = user_proj
    where = "user"

    # 2) dacă nu e user, încearcă organization
    if not proj:
        d = gql(token, q_org, {"owner": owner, "number": number})
        proj = (d.get("organization") or {}).get("projectV2")
        where = "org"

    if not proj:
        raise RuntimeError(f"Nu am găsit Project #{number} la '{owner}' (nici ca user, nici ca organization).")

    # opțional, mesaj de debug
    print(f"[INFO] Project găsit la {where}.")

    # Normalizează câmpurile într-un dict by-name
    field_by_name: Dict[str, Any] = {}
    for node in proj["fields"]["nodes"]:
        if not node or not node.get("name"):
            continue
        field = {
            "id": node.get("id"),
            "name": node.get("name"),
            "dataType": node.get("dataType"),
        }
        if node.get("__typename") == "ProjectV2SingleSelectField":
            field["options"] = node.get("options", []) or []
        field_by_name[field["name"]] = field

    return {"id": proj["id"], "fields": field_by_name}


def get_repo_id(token: str, repo_full: str) -> str:
    owner, name = repo_full.split("/", 1)
    q = """
    query($owner:String!, $name:String!) {
      repository(owner:$owner, name:$name){ id }
    }
    """
    d = gql(token, q, {"owner": owner, "name": name})
    repo = d["repository"]
    if not repo:
        raise RuntimeError(f"Repository-ul {repo_full} nu există sau nu ai acces.")
    return repo["id"]

def get_label_ids(token: str, repo_full: str, label_names: List[str]) -> List[str]:
    if not label_names:
        return []
    owner, name = repo_full.split("/", 1)
    q = """
    query($owner:String!, $name:String!, $query:String!) {
      repository(owner:$owner, name:$name){
        labels(first:100, query:$query){ nodes { id name } }
      }
    }
    """
    ids = []
    remaining = set([ln.strip() for ln in label_names if ln.strip()])
    # o singură interogare cu query gol ia primele 100; ca să acoperim orice, rulăm pe fiecare nume
    for ln in list(remaining):
        d = gql(token, q, {"owner": owner, "name": name, "query": ln})
        nodes = d["repository"]["labels"]["nodes"]
        match = next((x for x in nodes if x["name"].lower() == ln.lower()), None)
        if match:
            ids.append(match["id"])
            remaining.discard(ln)
    if remaining:
        print(f"[AVERTISMENT] Etichete inexistente în repo: {', '.join(sorted(remaining))}", file=sys.stderr)
    return ids

def get_user_ids(token: str, logins: List[str]) -> List[str]:
    if not logins:
        return []
    q = """
    query($login:String!){ user(login:$login){ id login } }
    """
    ids = []
    for lg in {x.strip() for x in logins if x.strip()}:
        d = gql(token, q, {"login": lg})
        u = d.get("user")
        if u:
            ids.append(u["id"])
        else:
            print(f"[AVERTISMENT] Utilizator inexistent: {lg}", file=sys.stderr)
    return ids

def create_issue(token: str, repo_id: str, title: str, body: str,
                 label_ids: List[str], assignee_ids: List[str]) -> str:
    m = """
    mutation($input:CreateIssueInput!){
      createIssue(input:$input){
        issue { id number url }
      }
    }
    """
    d = gql(token, m, {"input": {
        "repositoryId": repo_id,
        "title": title,
        "body": body or "",
        "labelIds": label_ids or None,
        "assigneeIds": assignee_ids or None
    }})
    issue = d["createIssue"]["issue"]
    print(f"Creat issue #{issue['number']}: {issue['url']}")
    return issue["id"]

def add_item_to_project(token: str, project_id: str, content_id: str) -> str:
    m = """
    mutation($projectId:ID!, $contentId:ID!){
      addProjectV2ItemById(input:{projectId:$projectId, contentId:$contentId}){
        item { id }
      }
    }
    """
    d = gql(token, m, {"projectId": project_id, "contentId": content_id})
    return d["addProjectV2ItemById"]["item"]["id"]

def update_field(token: str, project_id: str, item_id: str,
                 field_id: str, data_type: str, value: str, options: Optional[List[Dict[str,str]]] = None):
    # detect value payload by type
    val: Dict[str, Any]
    if data_type == "TEXT":
        val = {"text": value}
    elif data_type == "NUMBER":
        # try to coerce to number
        try:
            num = float(value)
        except ValueError:
            print(f"[AVERTISMENT] Câmp numeric ignorat (valoare invalidă): {value}", file=sys.stderr)
            return
        val = {"number": num}
    elif data_type == "DATE":
        # GitHub așteaptă YYYY-MM-DD
        val = {"date": value}
    elif data_type == "SINGLE_SELECT":
        if not options:
            print(f"[AVERTISMENT] Câmp single-select fără opțiuni; ignorat.", file=sys.stderr)
            return
        opt = next((o for o in options if o["name"].lower() == value.lower()), None)
        if not opt:
            print(f"[AVERTISMENT] Opțiune necunoscută '{value}' pentru câmpul single-select; ignorat.", file=sys.stderr)
            return
        val = {"singleSelectOptionId": opt["id"]}
    else:
        print(f"[INFO] Tip de câmp nesuportat acum: {data_type}; ignor.", file=sys.stderr)
        return

    m = """
    mutation($input:UpdateProjectV2ItemFieldValueInput!){
      updateProjectV2ItemFieldValue(input:$input){ projectV2Item { id } }
    }
    """
    gql(token, m, {"input": {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "value": val
    }})

def create_draft_issue_and_item(token: str, project_id: str, title: str, body: str,
                                assignee_ids: List[str]) -> str:
    m = """
    mutation($input:AddProjectV2DraftIssueInput!){
      addProjectV2DraftIssue(input:$input){
        projectItem { id }
      }
    }
    """
    d = gql(token, m, {"input": {
        "projectId": project_id,
        "title": title,
        "body": body or "",
        "assigneeIds": assignee_ids or None
    }})
    return d["addProjectV2DraftIssue"]["projectItem"]["id"]


def main():
    ap = argparse.ArgumentParser(description="Import CSV ca task-uri în GitHub Project (v2)")
    ap.add_argument("--token", help="GitHub token (sau setează env GITHUB_TOKEN)")
    ap.add_argument("--project-owner", required=True, help="Owner (org sau user) al Project-ului, ex: my-org sau my-user")
    ap.add_argument("--project-number", required=True, type=int, help="Numărul Project-ului (din URL)")
    ap.add_argument("--csv", required=True, help="Calea către fișierul CSV")
    ap.add_argument("--repo", help="Repo în care se creează issues, ex: org/repo (dacă lipsește și folosești --draft, vor fi Draft Issues)")
    ap.add_argument("--delimiter", default=",", help="Delimiter CSV (default ,)")
    ap.add_argument("--draft", action="store_true", help="Creează Draft Issues în Project în loc de Issues în repo")
    ap.add_argument("--rate-sleep", type=float, default=0.25, help="Pauză între mutații (sec)")
    args = ap.parse_args()

    token = args.token or os.getenv("GITHUB_TOKEN")
    if not token:
        print("Lipsește token-ul. Folosește --token sau variabila de mediu GITHUB_TOKEN.", file=sys.stderr)
        sys.exit(1)

    project = get_project_and_fields(token, args.project_owner, args.project_number)
    project_id = project["id"]
    project_fields: Dict[str, Any] = project["fields"]

    repo_id = None
    if not args.draft:
        if not args.repo:
            print("Pentru mod non-draft trebuie să specifici --repo (ex: org/repo). Sau folosește --draft.", file=sys.stderr)
            sys.exit(1)
        repo_id = get_repo_id(token, args.repo)

    with open(args.csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=args.delimiter)
        required = None
        for i, row in enumerate(reader, start=1):
            title = row.get("Title") or row.get("title")
            if not title:
                print(f"[EROARE] Linia {i}: lipsește coloana Title.", file=sys.stderr)
                continue
            body = row.get("Body") or row.get("body") or ""

            labels_raw = row.get("Labels") or row.get("labels") or ""
            labels = [x.strip() for x in labels_raw.split(",")] if labels_raw else []

            assignees_raw = row.get("Assignees") or row.get("assignees") or ""
            assignees = [x.strip().lstrip("@") for x in assignees_raw.split(",")] if assignees_raw else []

            assignee_ids = get_user_ids(token, assignees)

            if args.draft:
                # Create Draft Issue directly in Project
                item_id = create_draft_issue_and_item(token, project_id, title, body, assignee_ids)
                print(f"Draft Issue creat și adăugat în Project (item {item_id}).")
            else:
                # Create Issue in repo, then add it to Project
                label_ids = get_label_ids(token, args.repo, labels)
                issue_id = create_issue(token, repo_id, title, body, label_ids, assignee_ids)
                item_id = add_item_to_project(token, project_id, issue_id)
                print(f"Adăugat în Project item {item_id}.")

            # Setează câmpuri de Project pe baza coloanelor suplimentare
            for col_name, col_val in row.items():
                if col_name in ("Title", "title", "Body", "body", "Labels", "labels", "Assignees", "assignees"):
                    continue
                if col_val is None or str(col_val).strip() == "":
                    continue
                field = project_fields.get(col_name)
                if not field:
                    # ignoră coloane care nu corespund câmpurilor din Project
                    continue
                options = None
                if field["dataType"] == "SINGLE_SELECT":
                    options = field.get("options") or []
                try:
                    update_field(token, project_id, item_id, field["id"], field["dataType"], str(col_val).strip(), options)
                except Exception as e:
                    print(f"[AVERTISMENT] Nu am putut seta câmpul '{col_name}' la '{col_val}': {e}", file=sys.stderr)

            time.sleep(args.rate_sleep)

if __name__ == "__main__":
    main()

