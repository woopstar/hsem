# Agent Customization for HSEM

This document describes custom agents available for HSEM development tasks.

## Available Agents

### Explore Agent
**Purpose**: Fast read-only codebase exploration and Q&A

**When to use**:
- Understanding how existing features work
- Finding where to make changes
- Analyzing dependencies and relationships
- Gathering context before implementing changes

**Capabilities**:
- Quick keyword-based searches
- Medium thoroughness for targeted exploration
- Thorough codebase analysis for complex features

**Example queries**:
- "Where is the weighted values calculation implemented?"
- "How does the planner determine battery charging schedules?"
- "What sensors feed into the power prediction model?"

## Development Agents

### Primary Development Workflow
1. Use **Explore** agent to understand the codebase
2. Work directly with Claude Code or Copilot to implement changes
3. Run checks and tests locally
4. Create PR for review

## Best Practices

- Use agents for discovery and understanding
- Use direct coding for implementation
- Always run local checks before pushing
- Keep changes focused on one issue
- Document complex logic clearly

## Customization

Custom agents can be added to `.agent/` directory following the naming convention:
- File name: `<agent-name>.md`
- Must include agent purpose, capabilities, and example usage
- Link to agent from this file for discoverability
