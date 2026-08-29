-- Add missing validation columns to stock_quotes
ALTER TABLE stock_quotes
ADD COLUMN IF NOT EXISTS pe REAL,
ADD COLUMN IF NOT EXISTS pb REAL,
ADD COLUMN IF NOT EXISTS mkt_cap REAL;
