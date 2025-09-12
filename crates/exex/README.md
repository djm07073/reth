# Reth Execution Extensions (ExEx)

## Architecture Overview

ExEx (Execution Extensions) enables custom functionality to be added to the Reth pipeline. It allows developers to build custom indexers, rollups, bridges, and other state derivations that run alongside the main node.

## Architecture Diagram

```mermaid
flowchart TB
    subgraph RethNode["Reth Node"]
        subgraph CorePipeline["Core Pipeline"]
            BC[Blockchain Tree]
            SYNC[Sync Pipeline]
            ENGINE[Consensus Engine]
        end
        
        subgraph ExExManager["ExEx Manager"]
            EM[ExEx Manager]
            WAL["Write-Ahead Log<br/>WAL"]
            NOTIF["Notification Buffer<br/>max: 1024"]
        end
        
        subgraph ExExInstances["ExEx Instances"]
            EX1["ExEx 1<br/>e.g., Indexer"]
            EX2["ExEx 2<br/>e.g., Rollup"]
            EX3["ExEx N<br/>e.g., Bridge"]
        end
    end
    
    subgraph ExternalSystems["External Systems"]
        DB[(Custom DB)]
        API[External API]
        L2[L2 Network]
    end
    
    %% Core Pipeline to ExEx Manager
    BC -->|"BlockchainTree<br/>Notifications"| EM
    SYNC -->|"Pipeline<br/>Notifications"| EM
    ENGINE -->|"Fork Choice<br/>Updates"| EM
    
    %% ExEx Manager internal
    EM --> WAL
    EM --> NOTIF
    
    %% ExEx Manager to ExEx Instances
    NOTIF -->|CanonStateNotification| EX1
    NOTIF -->|CanonStateNotification| EX2
    NOTIF -->|CanonStateNotification| EX3
    
    %% ExEx feedback to Manager
    EX1 -->|"ExExEvent::FinishedHeight"| EM
    EX2 -->|"ExExEvent::FinishedHeight"| EM
    EX3 -->|"ExExEvent::FinishedHeight"| EM
    
    %% ExEx to External Systems
    EX1 -.->|"Index Data"| DB
    EX2 -.->|"State Updates"| L2
    EX3 -.->|"Bridge Events"| API
    
    style EM fill:#e1f5e1
    style WAL fill:#fff2cc
    style NOTIF fill:#fff2cc
    style EX1 fill:#d4e1f5
    style EX2 fill:#d4e1f5
    style EX3 fill:#d4e1f5
```

## Data Flow

```mermaid
sequenceDiagram
    participant Pipeline as Sync Pipeline
    participant Tree as Blockchain Tree
    participant Manager as ExEx Manager
    participant WAL as WAL Storage
    participant ExEx as ExEx Instance
    participant External as External System
    
    alt New Block from Sync
        Pipeline->>Manager: Pipeline Notification
        Note over Manager: No WAL commit<br/>(already finalized)
    else New Block from Tree
        Tree->>Manager: Tree Notification
        Manager->>WAL: Commit to WAL
    end
    
    Manager->>Manager: Buffer Notification<br/>(max 1024)
    Manager->>ExEx: Send CanonStateNotification
    
    ExEx->>ExEx: Process Block Data
    ExEx->>External: Write to External System
    ExEx->>Manager: ExExEvent::FinishedHeight(block_num)
    
    Manager->>Manager: Update Pruning Height
    Note over Manager: Prune state below<br/>min(all ExEx heights)
```

## Key Components

### 1. **ExEx Manager**

- Coordinates all ExEx instances
- Manages notification distribution
- Handles state pruning based on ExEx progress
- Maintains WAL for crash recovery

### 2. **Notification System**

- **CanonStateNotification**: Main notification type for state changes
- **Buffer**: Up to 1024 notifications (3.5 hours of mainnet blocks)
- **Source Types**:
  - Pipeline: Already finalized blocks
  - BlockchainTree: New blocks requiring WAL persistence

### 3. **WAL (Write-Ahead Log)**

- Ensures ExEx consistency during crashes
- Only stores BlockchainTree notifications
- Warning threshold: 128 blocks

### 4. **ExEx Context**

- Provides access to node components
- Notification stream for state updates
- Event channel for signaling progress

## State Management

```mermaid
stateDiagram-v2
    [*] --> Initialized: ExEx Started
    
    Initialized --> Listening: Subscribe to Notifications
    
    Listening --> Processing: Receive Notification
    
    Processing --> Processing: Process Block
    Processing --> Signaling: Block Processed
    
    Signaling --> Listening: Send FinishedHeight
    
    Listening --> [*]: Shutdown
    
    note right of Processing
        Custom logic:
        - Index data
        - Update rollup state
        - Bridge events
    end note
    
    note right of Signaling
        Critical for pruning!
        Must signal progress
    end note
```

## Example ExEx Implementation

```rust
async fn my_exex<N: FullNodeComponents>(
    mut ctx: ExExContext<N>,
) -> Result<(), Box<dyn std::error::Error>> {
    // Subscribe to notifications
    while let Some(Ok(notification)) = ctx.notifications.next().await {
        if let Some(committed) = notification.committed_chain() {
            for block in committed.blocks_iter() {
                // Custom processing logic
                process_block(block)?;
            }
            
            // Signal completion for pruning
            ctx.send_finished_height(committed.tip().num_hash());
        }
    }
    Ok(())
}
```

## Use Cases

| Use Case | Description | Example |
|----------|-------------|---------|
| **Indexers** | Index blockchain data for queries | Token transfers, NFT metadata |
| **Rollups** | Build L2 state from L1 data | Optimistic/ZK rollups |
| **Bridges** | Track cross-chain events | Token bridges, message passing |
| **Analytics** | Real-time blockchain analytics | MEV detection, gas analysis |
| **Oracles** | Provide off-chain data | Price feeds, event attestation |

## Best Practices

1. **Always emit FinishedHeight** - Critical for proper state pruning
2. **Handle errors gracefully** - ExEx should not crash the node
3. **Minimize blocking operations** - Use async operations
4. **Control resource usage** - Monitor memory and CPU
5. **Process notifications in order** - Maintain canonical consistency
