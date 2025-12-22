## gh-pages Configuration

After installation:
1. Create and push an orphaned `gh-pages` branch.
    ```bash
    git worktree add --orphan -b gh-pages _tmp_gh-pages
    cd _tmp_gh-pages
    git commit --allow-empty -m "Initial commit"
    git push -u origin gh-pages
    cd ..
    git worktree remove _tmp_gh-pages 
    ```

2. Configure github pages. Under settings:
    - set default branch to master
    - Build and deployment
        - Deploy from branch: `gh-pages` (root)

