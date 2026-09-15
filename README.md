# Rent Flow Common

This repository owns the local infrastructure topology shared by the Rent Flow services. Application-level
Kafka behavior remains in the service that publishes or consumes an event.

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


## Local Kafka

The Compose stack runs the official `apache/kafka:4.3.1` image as one combined KRaft broker/controller.
This topology is intentionally small and uses plaintext connections, so it is
appropriate for local development but not a production Kafka deployment.

### Start Kafka and wait until healthy

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh start
```

### Show status or list topics

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh status
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh topics
```

### Follow logs

Press <kbd>Ctrl</kbd>+<kbd>C</kbd> to stop following the logs.

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh logs
```

### Stop Kafka while preserving topics and messages

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh stop
```

### Reset Kafka

Delete every local topic and message and start a fresh broker. This command asks for confirmation and
removes only the Kafka volume.

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh reset
```

For a non-interactive destructive reset:

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh reset --force
```

### Show help

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh help
```

### Bootstrap addresses

Kafka advertises two client listeners because `localhost` means different things on the host and inside a
container.

| Client location | Bootstrap servers |
| --- | --- |
| Service started from an IDE or shell on the host | `localhost:9092` |
| Service container in this Compose project | `rentflow-kafka:19092` |

Set `KAFKA_PORT` before running the script to change the host port. For example,
`KAFKA_PORT=19092 ./scripts/local-kafka.sh start` makes the host bootstrap address `localhost:19092`.
Port `29093` is private controller traffic and must not be used by applications.

Automatic topic creation is disabled deliberately. A misspelled topic must fail visibly instead of silently
creating a topic with accidental defaults.
