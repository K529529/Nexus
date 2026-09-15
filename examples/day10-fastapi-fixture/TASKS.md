# Demonstration tasks

Each task starts from a new disposable copy of this directory and a clean initial Git commit.

1. **Bug fix:** Correct the premium large-order discount so the existing failing test passes.
2. **Feature modification:** Add an optional `vip` query parameter whose documented behavior is
   a 20 percent discount for non-negative orders, with focused tests.
3. **Behavior-preserving refactor:** Extract request validation from the `/discount` endpoint
   without changing responses, then run the full fixture suite.
4. **Missing targeted test:** Add a focused endpoint test proving a negative `order_total` returns
   HTTP 422 with the existing safe detail; do not change production behavior.
5. **Repository explanation:** Explain the request path from `/discount` through
   `discount_percent`, including validation and tests; make no files change.

The Day 10 release walkthrough uses task 1. Tasks 2-5 demonstrate that the same small fixture can
cover all Frozen Baseline categories; they are not additional release acceptance runs.
