# Instructions

- Download the dataset from 'data/source/data.txt'
- Create EC2 instance > docker container for SQL Server > mcr.microsoft.com/mssql/server:2022-latest
- Spin up the Legacy MSSQL Server
```
docker run -e "ACCEPT_EULA=Y" -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" -p 1433:1433 --name legacy-mssql -d mcr.microsoft.com/mssql/server:2022-latest

# or multi-line in windows 
docker run -e "ACCEPT_EULA=Y" -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" ^
   -p 1433:1433 --name legacy-mssql ^
   -d mcr.microsoft.com/mssql/server:2022-latest

# or multi-line unix
docker run -e "ACCEPT_EULA=Y" -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" \
   -p 1433:1433 --name legacy-mssql \
   -d mcr.microsoft.com/mssql/server:2022-latest

# or multi-line with volume inside EC2
docker run -v mssql_data:/var/opt/mssql \
  -e "ACCEPT_EULA=Y" \
  -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" \
  -p 1433:1433 \
  --name legacy-mssql \
  -d mcr.microsoft.com/mssql/server:2022-latest

```
- Install the requirements: pip install -r requirements.txt
- Load the legacy table (`dbo.TBL_SC_FLEET_HIST_RAW`):
  ```
  python scripts/ingest_legacy_data.py --dry-run      # validate the CSV only, no DB access
  python scripts/ingest_legacy_data.py                # atomic full refresh (staging table -> swap)
  python scripts/ingest_legacy_data.py --mode upsert  # MERGE new/changed rows on TS_UTC
  python scripts/ingest_legacy_data.py --force        # reload even if the CSV is unchanged
  ```
  - Required env: `SQL_ADMIN_USER`, `SQL_ADMIN_PASSWORD` (plus `SQL_SERVER_HOST`, `SQL_SERVER_PORT`).
  - Optional env: `SQL_SERVER_DATABASE` (default `master`), `SQL_ODBC_DRIVER` (default: newest installed
    `ODBC Driver NN for SQL Server`), `SQL_ENCRYPT`, `SQL_TRUST_SERVER_CERTIFICATE`, `RAW_DATA_PATH`,
    `CSV_CHUNK_ROWS`, `INSERT_BATCH_ROWS`, `MAX_REJECT_RATIO` (default `0.01`), `REJECT_DIR`.
  - Invalid rows are written to `data/rejects/<run_id>.csv`; the load aborts (live table untouched) if
    more than `MAX_REJECT_RATIO` of rows are rejected.
  - Exit codes: 0 ok/skipped, 1 load or validation failure, 2 config error, 3 another load is running.
  - Every run is recorded in `dbo.TBL_SC_INGEST_AUDIT`:
    `SELECT TOP 10 * FROM dbo.TBL_SC_INGEST_AUDIT ORDER BY STARTED_UTC DESC;`

#Connecting to the data
Download : https://github.com/microsoft/azuredatastudio
    - https://learn.microsoft.com/en-us/previous-versions/azure-data-studio/download-azure-data-studio?tabs=win-install%2Cwin-user-install%2Credhat-install%2Cwindows-uninstall%2Credhat-uninstall

- The recommendation is to use the VS Code extension: **"SQL Server (mssql)"** by Microsoft
  - Click on the SQL Server icon in the sidebar (looks like a server/refrigerator, not the cylinder icon)
  - Click **Add Connection** (`+`)
  - Fill in the connection details below:
    ```text
    Profile Name: legacy-mssql
    Server name*: localhost (or your EC2 public IP)
    Port: 1433
    Trust server certificate: 🟩 Check this box / turn it ON (Crucial for Docker)
    Authentication type*: SQL Login
    User name*: sa
    Password*: FdeEnterprisePass123!
    Save Password: 🟩 Check this box
    Database name: Type master (or leave it on "Select a database")
    Encrypt: ⚠️ Change this from Mandatory to Optional (or False)
    ```
- Right-click the connected `legacy-mssql` profile (or `master` database) and select **New Query** (or press `Cmd+N` / `Ctrl+N` and set language mode to `SQL`)
- In the query window, run the following queries (`Cmd+Shift+E` on macOS / `Ctrl+Shift+E` on Windows) to verify the connection and ingested data:
  ```sql
  -- 1. Check available databases
  SELECT * FROM sys.databases;

  -- 2. Verify total ingested rows (expected: 32,065 rows)
  SELECT COUNT(*) AS total_rows FROM dbo.TBL_SC_FLEET_HIST_RAW;

  -- 3. Preview sample legacy records
  SELECT TOP 10 * FROM dbo.TBL_SC_FLEET_HIST_RAW;
  ```
# Instruction for EC2 instance > database
- Instance Type: c7i-flex.large
- storage: 30GB
- ubuntu(linux)
- Security Group and attach the security group to the EC2 Instancw without fail
- Launch instance
- SSH using .pem file from personal system
- install the docker
- 
```
docker run -v mssql_data:/var/opt/mssql \
  -e "ACCEPT_EULA=Y" \
  -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" \
  -p 1433:1433 \
  --name legacy-mssql \
  -d mcr.microsoft.com/mssql/server:2022-latest
```
- copy the ip address of the EC2 instance(public) to connect to cloud legacy system


# Phase 1
- Get API key for Pinecone and GEMINI
- use it in .env
- run scripts/ingest_sop_pinecone.py

# Phase 2
## Data Security

- Click on file: scripts/setup_security_view.sql
- VS-code will show you start button directly on top else run like we were running the commands previously.
- Once done, create a new connection now with Agent-Profile

```
* Profile Name: agent-fde-ro
* Connection Group: Leace it on <Default>
* Input type: Select Parameters (DO not click "Load from Connection String", "Browse AZure", or "Browse Fabric")
* Server name*: localhost
* Port: 1433
* Trust server Certifficate: Check this box/Turn it ON
* Authentication type*: SQL Login
* User name*: USR_FDE_RO
* Password*: AgentPassword2026!
* Save Password: Check this box/Turn it ON
* Database name: Type master(or Click "Select a database" and Select master)
* Encrypt: Change this from Mandatory to Optional (or False)
```

Connect and test below commands:
```
-- Test 1: This should work perfectly (Access to clean view)
Select TOP 5 * FROM FDE_VIEWS.VM_ACTIVE_FLEET;
-- TEST 2: This should fail instantly (Access to raw legacy table is DENIED)
SELECT TOP 5 * FROM dbo.TBL_SC_FLEET_HIST_RAW;

## Phase 3 
```
run src/agent_tool.py
run src/orchestrator.py

Step-2: Pass below in the dispatcher >
- Question-1 (The "Domino Effect" Test)
```
Find any acitve shipments near Los Angeles( Latitude ~33.8, Longitude ~-118.1).
Check the local weather there, and tell me if the current cargo temperature violates the SOP for fresh persihables.
```
- Question-2 ("The Restraint" Test (No-Tool Routing))
```
I'm a new dispatcher on the night shift. Can you quickly explain the difference between a Tier 1 and Tier 2 esclaation?
```
## Phase 4
Go to VS-code > CTRL + N > 'Click on plain-text' and use 'sql'

Select the su user and write below:
CREATE TABLE FDE_VIEWS.AgentAuditLog(  
  LogID INT IDENTITY(1,1) PRIMARY KEY,
  Timestamp DATETIME DEFAULT GETDATE(),
  SessionID VARCHAR(50),
  NodeExecuted VARCHAR(50),
  ToolName VARCHAR(100),
  Content NVARCHAR(MAX) -- NVARCHAR to safely handle JSON strings and large LLM outputs
);

-- Grant the agent user permission to write only to this specific table
GRANT INSERT ON FDE_VIEWS.AgentAuditLog to USR_FDE_RO;
```
## Phase 5
streamlit run src/ui.py

## Phase 6
- Deployment
```
sudo apt update && sudo apt install -y python3-pip python3-venv git
cd /home/ubuntu
git cone https://github.com/nimowhyca/cold-chain-logistics-FDE-Project.git
cd cold-chain-logistics-AI-Assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run src/ui.py
```

- Add Microsoft GPG key
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg

- Add the Microsoft Ubuntu repository (auto-detects Ubuntu 22.04 or 24.04)
curl -fsSL https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list

- Update apt and install ODBC Driver 18 + unixODBC dev headers
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev

- Install the unixODBC dev headers
sudo apt-get install unixodbc-dev