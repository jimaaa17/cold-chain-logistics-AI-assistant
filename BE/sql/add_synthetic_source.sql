-- ====================================================================
-- Adds the synthetic telemetry table and the Data_Source column to the agent's view.
-- Safe to run more than once. Run as admin, on databases set up before
-- this file existed (setup_security_view.sql now includes the same table and view).
-- Run it BEFORE deploying code that reads Data_Source.
-- The synthetic table stays empty until `python -m data_synth --load`.
-- ====================================================================

-- 1. Same columns as dbo.TBL_SC_FLEET_HIST_RAW (BE/legacy_ingest/schema.py, checked by tests)
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

-- 2. CREATE OR ALTER keeps the grants on the view
CREATE OR ALTER VIEW FDE_VIEWS.VW_ACTIVE_FLEET AS
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

-- 3. The agent reads the view only
DENY SELECT ON dbo.TBL_SC_FLEET_SYNTH_RAW TO USR_FDE_RO;
GO
