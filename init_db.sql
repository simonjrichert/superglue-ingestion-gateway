CREATE TABLE legacy_customers (
    id SERIAL PRIMARY KEY,
    raw_name VARCHAR(255),
    email VARCHAR(255),
    account_status VARCHAR(50),
    credit_card VARCHAR(100),
    created_at VARCHAR(100),
    notes VARCHAR(500)
);

INSERT INTO legacy_customers (raw_name, email, account_status, credit_card, created_at, notes) VALUES
('Acme Corp', 'contact@acme.com', 'ACTIVE', '4111-2222-3333-4444', '2026-01-15', NULL),
('Globex Inc', 'invalid-email-format', 'active', '4555-6666-7777-8888', '2026-02-01', NULL),
('Initech LLC', 'support@initech.io', 'PENDING', '4999-0000-1111-2222', '2026-02-10', NULL),
('Stark Ind', 'tony@stark.com', 'UNKNOWN_STATUS', '4333-2222-1111-0000', 'invalid-date', NULL),
('Umbrella Corp', 'info@umbrella.com', 'INACTIVE', '4000-1234-5678-9010', '2026-03-01', NULL),
('  Soylent Corp  ', 'hello@soylent.com', 'ACTIVE', '4000-0000-0000-0002', '2026-03-20', 'Leading/trailing whitespace in name'),
('Hooli', 'ops@hooli.xyz', 'ACTIVE', '4000-0000-0000-0003', '15/03/2026', 'European created_at'),
('Massive Dynamic', 'N/A', 'ACTIVE', '4000-0000-0000-0004', '2026-04-01', 'Placeholder email'),
('Tyrell Corp', 'ellen@tyrell.com', 'ACTIVE', NULL, '2026-04-12', 'Call billing about 4111-2222-3333-9999 before go-live'),
('', 'facilities@wayne.enterprises', 'INACTIVE', '4000-0000-0000-0006', '2026-05-01', 'Blank company name'),
('Acme Corp', 'orders@oscorp.io', 'ACTIVE', '4000-0000-0000-0007', '2026-05-18', 'Same Name as Acme; different email — do not merge');
