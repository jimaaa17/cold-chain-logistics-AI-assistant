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
- python scripts/ingest_legacy_data.py

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
ocker run -v mssql_data:/var/opt/mssql \
  -e "ACCEPT_EULA=Y" \
  -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" \
  -p 1433:1433 \
  --name legacy-mssql \
  -d mcr.microsoft.com/mssql/server:2022-latest
```
- copy the ip address of the EC2 instance(public) to connect to cloud legacy system