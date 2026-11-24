-- ====================================================================
-- FDE ENTERPRISE SECURITY & SEMANTIC LAYER SCRIPT
-- Purpose: Protect the legacy DB from LLM hallucinations and mutations
-- ====================================================================

-- 1. Create a dedicated schema for our clean AI views
CREATE SCHEMA FDE_VIEWS;
GO -- GO only works inside Microsoft terminal tools.

-- 2. Create the (empty) synthetic table and the Semantic View (Translating legacy junk to clean English)
IF OBJECT_ID(N'dbo.TBL_SC_FLEET_SYNTH_RAW', N'U') IS NULL
CREATE TABLE [dbo].[TBL_SC_FLEET_SYNTH_RAW] (
    [TS_UTC] DATETIME2(0) NOT NULL CONSTRAINT [PK_TBL_SC_FLEET_SYNTH_RAW] PRIMARY KEY CLUSTERED,
    [V_LAT] FLOAT NOT NULL,
    [V_LON] FLOAT NOT NULL,
    [IOT_TEMP_VAL_C] FLOAT NOT NULL,
    [CGO_COND_CD] FLOAT NOT NULL,
    [RISK_CLS_TXT] VARCHAR(20) NOT NULL,
    [DELAY_PROB_DEC] FLOAT NOT NULL,
    [PRT_CNG_LVL] FLOAT NOT NULL,
    [RT_RSK_IDX] FLOAT NOT NULL,
    [SYS_INGEST_FLAG] CHAR(1) NOT NULL CONSTRAINT [DF_TBL_SC_FLEET_SYNTH_RAW_FLAG] DEFAULT 'Y',
    CONSTRAINT [CK_TBL_SC_FLEET_SYNTH_RAW_RISK] CHECK ([RISK_CLS_TXT] IN ('Low Risk', 'Moderate Risk', 'High Risk')),
    CONSTRAINT [CK_TBL_SC_FLEET_SYNTH_RAW_FLAG] CHECK ([SYS_INGEST_FLAG] IN ('Y', 'N'))
);
GO

CREATE VIEW FDE_VIEWS.VW_ACTIVE_FLEET AS
SELECT
    TS_UTC AS [Timestamp],
    V_LAT AS [Latitude],
    V_LON AS [Longitude],
    CAST(IOT_TEMP_VAL_C AS FLOAT) AS [Current_Temperature_C],
    CGO_COND_CD AS [Cargo_Condition_Code],
    RISK_CLS_TXT AS [Risk_Classification],
    DELAY_PROB_DEC AS [Delay_Probability],
    PRT_CNG_LVL AS [Port_Congestion_Level],
    RT_RSK_IDX AS [Route_Risk_Index],
    CAST('historical' AS VARCHAR(10)) AS [Data_Source]
FROM dbo.TBL_SC_FLEET_HIST_RAW
UNION ALL
SELECT
    TS_UTC,
    V_LAT,
    V_LON,
    CAST(IOT_TEMP_VAL_C AS FLOAT),
    CGO_COND_CD,
    RISK_CLS_TXT,
    DELAY_PROB_DEC,
    PRT_CNG_LVL,
    RT_RSK_IDX,
    CAST('synthetic' AS VARCHAR(10))
FROM dbo.TBL_SC_FLEET_SYNTH_RAW;
GO

-- 3. Create a strict Read-Only Login and User for the AI Agent
-- The login gets a random password that nobody knows. Set a usable one afterwards with:
--     python scripts/rotate_agent_password.py --yes
DECLARE @pw NVARCHAR(128) = N'Aa1!' + CONVERT(NVARCHAR(64), CRYPT_GEN_RANDOM(24), 2);
EXEC (N'CREATE LOGIN USR_FDE_RO WITH PASSWORD = ''' + @pw + N''';');
GO

CREATE USER USR_FDE_RO FOR LOGIN USR_FDE_RO;
GO

-- 4. Grant access ONLY to the semantic view, explicitly denying everything else
GRANT SELECT ON FDE_VIEWS.VW_ACTIVE_FLEET TO USR_FDE_RO;
DENY SELECT ON dbo.TBL_SC_FLEET_HIST_RAW TO USR_FDE_RO;
DENY SELECT ON dbo.TBL_SC_FLEET_SYNTH_RAW TO USR_FDE_RO;
DENY INSERT, UPDATE, DELETE, ALTER ON SCHEMA::dbo TO USR_FDE_RO;
GO

-- 5. Create Audit Log Table & Grant Insert-Only Permission to Agent User
CREATE TABLE FDE_VIEWS.AgentAuditLog(
    LogID INT IDENTITY(1,1) PRIMARY KEY,
    Timestamp DATETIME DEFAULT GETDATE(),
    SessionID VARCHAR(50),
    NodeExecuted VARCHAR(50),
    ToolName VARCHAR(100),
    Content NVARCHAR(MAX) -- NVARCHAR to safely handle JSON strings and large LLM outputs
);
GO

GRANT INSERT ON FDE_VIEWS.AgentAuditLog TO USR_FDE_RO;
GO

-- # About line 33 :
-- ON SCHEMA::dbo: The dbo (Database Owner) schema is the default folder structure where your python ingestion script just dumped the TBL_SC_FLEET_HIST_RAW table. This target applies the rules to every single table or view currently inside dbo, or any tables you might add there in the future.