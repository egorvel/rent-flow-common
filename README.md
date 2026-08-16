# Rent Flow Common

## Local PostgreSQL startup

### Start PostgreSQL and wait until healthy

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh start
```

### Show status

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh status
```

### Follow logs

Press <kbd>Ctrl</kbd>+<kbd>C</kbd> to stop following the logs.

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh logs
```

### Stop PostgreSQL while preserving data

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh stop
```

### Reset the database

Delete the database volume and start fresh. This command asks for confirmation.

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh reset
```

For a non-interactive destructive reset:

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh reset --force
```

### Show help

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh help
```
