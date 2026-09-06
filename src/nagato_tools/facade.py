"""
Tool Facade - Unified Interface for Calling functions_internal

Provides a single entry point to call any nagato_* tool
with session context (full tracking) or in standalone mode.
"""

import asyncio
import inspect
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union, Set

from nagato_tools.ctx_mock import MockFSMContext, get_mock_context, _resolve_session_id

try:
    from nagato_tools.input_repair import (
        strip_noise_from_payload,
        repair_malformed_json,
        coerce_primitives,
        resolve_file_path,
    )
    _HAS_INPUT_REPAIR = True
except ImportError:
    _HAS_INPUT_REPAIR = False

from nagato_tools.tool_categories import ToolCategory


class ToolFacade:
    """Unified interface for calling nagato_tools with or without a session.
    
    Auto-detects session availability and injects the appropriate context.
    Provides both sync and async calling interfaces.
    
    Standalone mode supports tool filtering via:
    - Constructor parameters (explicit, highest priority)
    - Config file: .nagato/standalone.yaml (defaults, lower priority)
    - Runtime methods: enable_tool(), disable_tool(), etc.
    """
    
    def __init__(
        self, 
        fsm_instance: Optional[Any] = None, 
        workspace_root: Optional[Path] = None,
        mock_context: Optional[MockFSMContext] = None,
        session_id: Optional[str] = None,
        # Standalone tool filtering (only used when fsm_instance is None)
        allowed_tools: Optional[Set[str]] = None,
        denied_tools: Optional[Set[str]] = None,
        allowed_categories: Optional[List[ToolCategory]] = None,
    ):
        """Initialize the facade.
        
        Args:
            fsm_instance: Optional NagatoFSM instance. If provided, uses its Context.
            workspace_root: Workspace root path. Defaults to repo root.
            mock_context: Optional custom MockFSMContext. Created if not provided.
            session_id: Optional session ID for standalone mode. If not provided,
                       resolves from NAGATO_SESSION_ID env var or defaults to "standalone".
                       Formatted IDs like "proj:my-app" are sanitized for filesystem use.
            allowed_tools: Allowlist of tool names (e.g., {"nagato_read_file"}). 
                          Only these tools will be registered (subject to denied_tools).
            denied_tools: Denylist of tool names (e.g., {"nagato_shell"}). 
                         Takes precedence over allowed_tools and allowed_categories.
            allowed_categories: Allowlist of ToolCategory enums (e.g., [ToolCategory.READ]).
                               Only tools from these categories will be registered.
        """
        self._fsm = fsm_instance
        self._workspace_root = Path(workspace_root) if workspace_root is not None else Path(__file__).parents[1]
        
        # Resolve session_id for standalone mode
        resolved_session_id = _resolve_session_id(session_id) if fsm_instance is None else None
        
        if mock_context is not None:
            self._mock_context = mock_context
        elif session_id is not None or workspace_root is not None:
            self._mock_context = MockFSMContext(self._workspace_root, resolved_session_id)
        else:
            self._mock_context = get_mock_context(self._workspace_root, resolved_session_id)
        self._registry: Dict[str, Callable] = {}
        self._async_functions: set = set()
        self._tool_modules: Dict[str, str] = {}  # tool_name -> module_name
        
        # Tool filtering config (only for standalone mode)
        self._filter_allowed_tools: Set[str] = set(allowed_tools) if allowed_tools else set()
        self._filter_denied_tools: Set[str] = set(denied_tools) if denied_tools else set()
        self._filter_allowed_categories: Set[ToolCategory] = set(allowed_categories) if allowed_categories else set()
        self._filter_config_loaded: bool = False
        
        self._build_registry()
    
    @property
    def context(self) -> Union[Any, MockFSMContext]:
        """Get the active context (session.Context or MockContext)."""
        if self._fsm is not None and hasattr(self._fsm, 'Context'):
            return self._fsm.Context
        return self._mock_context
    
    @property
    def has_fsm(self) -> bool:
        """Check if a session is available."""
        return self._fsm is not None
    
    @property
    def workspace_root(self) -> Path:
        """Get workspace root."""
        return self._workspace_root
    
    def _load_standalone_config(self) -> None:
        """Load tool filtering config from .nagato/standalone.yaml if not explicitly provided."""
        if self._filter_config_loaded or self._fsm is not None:
            return  # Only for standalone mode, and only once
        
        # Only load config if no explicit filters were provided
        if not self._filter_allowed_tools and not self._filter_denied_tools and not self._filter_allowed_categories:
            try:
                from nagato_tools.config import get_standalone_config
                config = get_standalone_config()
                if config:
                    if "allowed_tools" in config:
                        self._filter_allowed_tools = set(config["allowed_tools"])
                    if "denied_tools" in config:
                        self._filter_denied_tools = set(config["denied_tools"])
                    if "allowed_categories" in config:
                        # Convert string category names to ToolCategory enums
                        for cat_str in config["allowed_categories"]:
                            try:
                                self._filter_allowed_categories.add(ToolCategory[cat_str.upper()])
                            except KeyError:
                                pass  # Ignore unknown categories
            except Exception:
                pass  # Config loading failed, continue without filtering
        
        self._filter_config_loaded = True
    
    def _get_tool_category(self, module_name: str, func_name: str) -> Optional[ToolCategory]:
        """Map a function to its ToolCategory based on module name."""
        # Module name to category mapping
        module_to_category = {
            'edit': ToolCategory.EDIT,
            'execute': ToolCategory.EXECUTE,
            'search': ToolCategory.SEARCH,
            'read': ToolCategory.READ,
            'lint': ToolCategory.TESTING,  # lint is testing-adjacent
            'git': ToolCategory.GIT,
            'web': ToolCategory.WEB,
            'test': ToolCategory.TESTING,
            'subgoals': ToolCategory.PLANNING,
            'create': ToolCategory.CREATION,
            'debugger': ToolCategory.DEBUGGING,
            'extractsignature': ToolCategory.READ,
            'extractcallgraph': ToolCategory.READ,
            'semanticindex': ToolCategory.SEARCH,
            'errors': ToolCategory.SYSTEM,
            'system': ToolCategory.SYSTEM,
            'shell': ToolCategory.SHELL,
            'monitor': ToolCategory.SYSTEM,
            'undo': ToolCategory.EDIT,  # undo modifies state
        }
        return module_to_category.get(module_name)
    
    def _should_register_tool(self, func_name: str, module_name: str) -> bool:
        """Check if a tool should be registered based on filters."""
        # Denylist takes absolute precedence
        if func_name in self._filter_denied_tools:
            return False
        
        # If allowlist is specified, tool must be in it
        if self._filter_allowed_tools and func_name not in self._filter_allowed_tools:
            return False
        
        # If allowed_categories is specified, tool's category must be in it
        if self._filter_allowed_categories:
            cat = self._get_tool_category(module_name, func_name)
            if cat is None or cat not in self._filter_allowed_categories:
                return False
        
        return True
    
    def _build_registry(self) -> None:
        """Auto-discover and register all nagato_* functions from nagato_tools."""
        import nagato_tools
        
        # List of modules to scan
        modules_to_scan = ['edit', 'execute', 'search', 'read', 'lint', 'git', 'web', 'services', 'test', 'subgoals', 'create', 'debugger', 'extractsignature', 'extractcallgraph', 'semanticindex', 'errors', 'system', 'config', 'undo', 'shell', 'monitor', 'token_calculator']
        
        for module_name in modules_to_scan:
            try:
                module = getattr(nagato_tools, module_name)
                self._register_module_functions(module_name, module)
            except (ImportError, AttributeError) as e:
                # Module might not exist or not have nagato_* functions
                pass
    def _register_module_functions(self, module_name: str, module: Any) -> None:
        """Register all nagato_* functions from a module, applying filters.

        Only public names (no leading underscore) are registered. Internal
        helpers such as `_nagato_error` are intentionally skipped. Re-exports
        of helpers under a public name (e.g. `nagato_error = _nagato_error`
        in a consumer module) are also skipped — a real tool function is
        *defined* in its own module, not aliased from another.
        """
        for name in dir(module):
            if not name.startswith('nagato_'):
                continue
            if name.startswith('_'):
                # Internal helper (e.g. `_nagato_error`) — not a tool.
                continue
            # Apply filters at registration time
            if not self._should_register_tool(name, module_name):
                continue

            func = getattr(module, name)
            if not callable(func):
                continue

            # Skip re-exports of helpers from other modules. A real tool is
            # defined in its own module; aliases imported via
            # `from x import helper as nagato_foo` carry a different __module__.
            func_module = getattr(func, "__module__", None)
            if func_module and func_module != module_name:
                # Special case: handlers etc. registered under a different name.
                # Accept only when func_module ends in module_name OR is a known
                # facade-style re-export — for tools, the canonical home wins.
                if not func_module.endswith(f".{module_name}") and not func_module.endswith(module_name):
                    continue

            # Check if it's async
            if inspect.iscoroutinefunction(func):
                self._async_functions.add(name)
            self._registry[name] = func
            self._tool_modules[name] = module_name
    
    def get_registered_functions(self) -> List[str]:
        """Get list of all registered function names."""
        return sorted(self._registry.keys())
    
    def is_async(self, func_name: str) -> bool:
        """Check if a function is async."""
        return func_name in self._async_functions
    

    def _preprocess_arguments(self, func, kwargs: dict) -> dict:
        if not _HAS_INPUT_REPAIR:
            return kwargs
        sig = inspect.signature(func)
        result = dict(kwargs)
        for key, value in list(result.items()):
            if isinstance(value, str):
                result[key] = strip_noise_from_payload(value)
        # Unwrap only when the SINGLE non-_ctx kwarg is in a whitelist of
        # obvious payload keys. Otherwise string params that happen to contain
        # valid JSON (e.g. code="{"k":1}") would get clobbered into the
        # entire kwargs dict, dropping every other parameter.
        _UNWRAP_KEYS = frozenset({"payload", "args", "kwargs", "data", "params"})
        non_ctx = {k: v for k, v in result.items() if k != "_ctx"}
        if len(non_ctx) == 1:
            (key, value) = next(iter(non_ctx.items()))
            if key in _UNWRAP_KEYS and isinstance(value, str) and value.strip().startswith("{"):
                repaired = repair_malformed_json(value)
                if repaired and isinstance(repaired, dict):
                    if "_ctx" in result:
                        repaired["_ctx"] = result["_ctx"]
                    result = repaired

        # Fuzzy-resolve file-path params that don't exist on disk
        for key, value in list(result.items()):
            if isinstance(value, str) and ("file" in key or key.endswith("_path")):
                result[key] = resolve_file_path(value, self._workspace_root)

        return coerce_primitives(result, sig)

    def call(self, func_name: str, *args, **kwargs) -> Any:
        """Call a registered function synchronously.
        
        Args:
            func_name: Name of the function to call (e.g., 'nagato_edit').
            *args: Positional arguments to pass to the function.
            **kwargs: Keyword arguments to pass to the function.
            
        Returns:
            Function result.
            
        Raises:
            ValueError: If function not found.
            RuntimeError: If trying to call async function synchronously.
        """
        func = self._registry.get(func_name)
        if func is None:
            available = ', '.join(sorted(self._registry.keys()))
            raise ValueError(f"Function '{func_name}' not found. Available: {available}")
        
        if self.is_async(func_name):
            raise RuntimeError(
                f"Function '{func_name}' is async. Use 'acall' or 'await facade.acall(...)'."
            )
        
        # Inject context if the function accepts it and it is not already bound
        sig = inspect.signature(func)
        if '_ctx' in sig.parameters:
            bound = sig.bind_partial(*args, **kwargs)
            if '_ctx' not in bound.arguments:
                kwargs['_ctx'] = self.context
        kwargs = self._preprocess_arguments(func, kwargs)

        import time
        start_t = time.time()
        err_msg = None
        result = None
        action_ctx = None
        if self._fsm is None and getattr(self.context, "active_action", None) is None:
            if func_name in ("nagato_edit", "nagato_edit_lines", "nagato_delete", "nagato_concept_create", "nagato_concept_link", "nagato_concept_collapse", "nagato_block_save", "nagato_block_delete"):
                try:
                    from nagato_tools.action_journal import ActionJournal
                    sess_id = getattr(self.context, "session_id", "standalone")
                    journal_dir = self._workspace_root / ".nagato" / "sessions" / sess_id / "action_journal"
                    aj = ActionJournal(journal_dir, self._workspace_root, session_id=sess_id)
                    clean_kwargs = {k: str(v) for k, v in kwargs.items() if k != '_ctx'}
                    args_preview = str(clean_kwargs or [str(a) for a in args])[:200]
                    action_ctx = aj.begin_action(func_name, args_preview=args_preview)
                    self.context.active_action = action_ctx
                except Exception:
                    pass

        try:
            result = func(*args, **kwargs)
            if action_ctx is not None:
                try:
                    action_ctx.commit()
                except Exception:
                    pass
                self.context.active_action = None
            return result
        except Exception as exc:
            err_msg = str(exc)
            if action_ctx is not None:
                try:
                    action_ctx.abort()
                except Exception:
                    pass
                self.context.active_action = None
            raise
        finally:
            # Emit telemetry event in standalone mode if no active FSM session is managing it
            if self._fsm is None:
                try:
                    from nagato_tools.telemetry import AuditEvent, EventType, TelemetryMode
                    from nagato_tools.telemetry import publish_event
                    dur_ms = round((time.time() - start_t) * 1000, 2)
                    clean_kwargs = {k: str(v) for k, v in kwargs.items() if k != '_ctx'}
                    publish_event(
                        AuditEvent(
                            session_id="standalone",
                            mode=TelemetryMode.STANDALONE,
                            event_type=EventType.TOOL_DISPATCH,
                            duration_ms=dur_ms,
                            payload={
                                "tool": func_name,
                                "args": clean_kwargs if clean_kwargs else [str(a) for a in args],
                                "result": str(result)[:2000] if result is not None else None,
                            },
                            error=err_msg,
                        ),
                        workspace_root=self._workspace_root,
                    )
                except Exception:
                    pass
    
    async def acall(self, func_name: str, *args, **kwargs) -> Any:
        """Call a registered function asynchronously.
        
        Args:
            func_name: Name of the function to call (e.g., 'nagato_edit').
            *args: Positional arguments to pass to the function.
            **kwargs: Keyword arguments to pass to the function.
            
        Returns:
            Function result.
            
        Raises:
            ValueError: If function not found.
        """
        func = self._registry.get(func_name)
        if func is None:
            available = ', '.join(sorted(self._registry.keys()))
            raise ValueError(f"Function '{func_name}' not found. Available: {available}")
        
        # Inject context if the function accepts it and it is not already bound
        sig = inspect.signature(func)
        if '_ctx' in sig.parameters:
            bound = sig.bind_partial(*args, **kwargs)
            if '_ctx' not in bound.arguments:
                kwargs['_ctx'] = self.context
        kwargs = self._preprocess_arguments(func, kwargs)

        import time
        start_t = time.time()
        err_msg = None
        result = None
        action_ctx = None
        if self._fsm is None and getattr(self.context, "active_action", None) is None:
            if func_name in ("nagato_edit", "nagato_edit_lines", "nagato_delete", "nagato_concept_create", "nagato_concept_link", "nagato_concept_collapse", "nagato_block_save", "nagato_block_delete"):
                try:
                    from nagato_tools.action_journal import ActionJournal
                    sess_id = getattr(self.context, "session_id", "standalone")
                    journal_dir = self._workspace_root / ".nagato" / "sessions" / sess_id / "action_journal"
                    aj = ActionJournal(journal_dir, self._workspace_root, session_id=sess_id)
                    clean_kwargs = {k: str(v) for k, v in kwargs.items() if k != '_ctx'}
                    args_preview = str(clean_kwargs or [str(a) for a in args])[:200]
                    action_ctx = aj.begin_action(func_name, args_preview=args_preview)
                    self.context.active_action = action_ctx
                except Exception:
                    pass

        try:
            if self.is_async(func_name):
                result = await func(*args, **kwargs)
            else:
                # Run sync function in thread pool to avoid blocking.
                # NOTE: use get_running_loop() (not the deprecated get_event_loop())
                # to silence DeprecationWarning on Python 3.12+ when called
                # without a running loop in the caller.
                running_loop = asyncio.get_running_loop()
                result = await running_loop.run_in_executor(None, lambda: func(*args, **kwargs))
            if action_ctx is not None:
                try:
                    action_ctx.commit()
                except Exception:
                    pass
                self.context.active_action = None
            return result
        except Exception as exc:
            err_msg = str(exc)
            if action_ctx is not None:
                try:
                    action_ctx.abort()
                except Exception:
                    pass
                self.context.active_action = None
            raise
        finally:
            if self._fsm is None:
                try:
                    from nagato_tools.telemetry import AuditEvent, EventType, TelemetryMode
                    from nagato_tools.telemetry import publish_event
                    dur_ms = round((time.time() - start_t) * 1000, 2)
                    clean_kwargs = {k: str(v) for k, v in kwargs.items() if k != '_ctx'}
                    publish_event(
                        AuditEvent(
                            session_id="standalone",
                            mode=TelemetryMode.STANDALONE,
                            event_type=EventType.TOOL_DISPATCH,
                            duration_ms=dur_ms,
                            payload={
                                "tool": func_name,
                                "args": clean_kwargs if clean_kwargs else [str(a) for a in args],
                                "result": str(result)[:2000] if result is not None else None,
                            },
                            error=err_msg,
                        ),
                        workspace_root=self._workspace_root,
                    )
                except Exception:
                    pass
    
    def __getattr__(self, name: str) -> Callable:
        """Allow attribute-style access: facade.nagato_edit(...)."""
        if name in self._registry:
            func = self._registry[name]
            
            if self.is_async(name):
                async def async_wrapper(**kwargs):
                    return await self.acall(name, **kwargs)
                return async_wrapper
            else:
                def sync_wrapper(**kwargs):
                    return self.call(name, **kwargs)
                return sync_wrapper
        
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")
    
    def __dir__(self) -> List[str]:
        """Include registered functions in dir()."""
        return super().__dir__() + self.get_registered_functions()
    
    # =========================================================================
    # Runtime Tool Filtering Methods (Standalone Mode Only)
    # =========================================================================
    
    def enable_tool(self, tool_name: str) -> bool:
        """Enable a previously disabled tool at runtime.
        
        Args:
            tool_name: Name of the tool to enable (e.g., "nagato_shell")
            
        Returns:
            True if tool was enabled, False if the tool does not exist or in session mode
        """
        if self._fsm is not None:
            return False  # Not available in session mode
        
        if tool_name in self._filter_denied_tools:
            self._filter_denied_tools.discard(tool_name)
            # Re-build registry to include the tool
            self._rebuild_registry()
            return True
        return False
    
    def disable_tool(self, tool_name: str) -> bool:
        """Disable a tool at runtime.
        
        Args:
            tool_name: Name of the tool to disable (e.g., "nagato_shell")
            
        Returns:
            True if tool was disabled, False if the tool does not exist or in session mode
        """
        if self._fsm is not None:
            return False  # Not available in session mode
        
        if tool_name in self._registry:
            self._filter_denied_tools.add(tool_name)
            # Remove from registry immediately
            self._registry.pop(tool_name, None)
            self._async_functions.discard(tool_name)
            self._tool_modules.pop(tool_name, None)
            return True
        return False
    
    def enable_category(self, category: ToolCategory) -> int:
        """Enable all tools in a category at runtime.
        
        Args:
            category: ToolCategory to enable
            
        Returns:
            Number of tools enabled
        """
        if self._fsm is not None:
            return 0
        
        # Remove category from allowed_categories filter (if it was restricting)
        # This is a bit complex - we'd need to re-evaluate all tools
        # For simplicity, rebuild registry with updated allowed_categories
        self._filter_allowed_categories.discard(category)
        self._rebuild_registry()
        return len(self._registry)
    
    def disable_category(self, category: ToolCategory) -> int:
        """Disable all tools in a category at runtime.
        
        Args:
            category: ToolCategory to disable
            
        Returns:
            Number of tools disabled
        """
        if self._fsm is not None:
            return 0
        
        # Add category to a "denied_categories" concept
        # For now, we'll add all tools in that category to denied_tools
        disabled_count = 0
        for tool_name in list(self._registry.keys()):
            cat = self._get_tool_category_for_tool(tool_name)
            if cat == category:
                self.disable_tool(tool_name)
                disabled_count += 1
        return disabled_count
    
    def _get_tool_category_for_tool(self, tool_name: str) -> Optional[ToolCategory]:
        """Get category for a registered tool by looking up its source module.

        Delegates to `_get_tool_category(module_name)` which holds the
        canonical module→category mapping.
        """
        module_name = self._tool_modules.get(tool_name)
        if module_name is None:
            return None
        return self._get_tool_category(module_name)
    
    def _rebuild_registry(self) -> None:
        """Rebuild the entire registry from scratch (used after filter changes)."""
        self._registry.clear()
        self._async_functions.clear()
        self._tool_modules.clear()
        self._filter_config_loaded = False  # Force reload
        self._build_registry()
    
    def set_tool_filter(self, predicate: Callable[[str], bool]) -> None:
        """Set a custom filter predicate for tool registration.
        
        Args:
            predicate: Function taking tool_name -> bool (True = allow)
        """
        if self._fsm is not None:
            return
        
        # Store predicate and rebuild
        self._custom_filter = predicate
        self._rebuild_registry()
    
    def get_filter_status(self) -> dict:
        """Get current filter configuration status.
        
        Returns:
            Dict with current filter settings
        """
        return {
            "allowed_tools": sorted(self._filter_allowed_tools),
            "denied_tools": sorted(self._filter_denied_tools),
            "allowed_categories": [c.name for c in self._filter_allowed_categories],
            "registered_tools": self.get_registered_functions(),
            "config_loaded": self._filter_config_loaded,
        }


# Global facade instance for simple access
_global_tool_facade: Optional[ToolFacade] = None


def get_tool_facade(
    fsm_instance: Optional[Any] = None,
    workspace_root: Optional[Path] = None,
    mock_context: Optional[MockFSMContext] = None,
    session_id: Optional[str] = None,
) -> ToolFacade:
    """Get or create the global facade instance.
    
    Args:
        fsm_instance: Optional NagatoFSM instance.
        workspace_root: Workspace root path.
        mock_context: Optional custom MockFSMContext.
        session_id: Optional session ID for standalone mode. If not provided,
                   resolves from NAGATO_SESSION_ID env var or defaults to "standalone".
        
    Returns:
        ToolFacade instance.
    """
    global _global_tool_facade
    if _global_tool_facade is None:
        _global_tool_facade = ToolFacade(fsm_instance, workspace_root, mock_context, session_id)
    elif fsm_instance is not None and _global_tool_facade._fsm is None:
        # Upgrade to session mode if the facade was created without a session
        _global_tool_facade._fsm = fsm_instance
    return _global_tool_facade


def set_facade(facade: ToolFacade) -> None:
    """Set a custom global facade instance."""
    global _global_tool_facade
    _global_tool_facade = facade


def create_standalone_facade(
    workspace_root: Optional[Path] = None,
    session_id: Optional[str] = None,
    allowed_tools: Optional[Set[str]] = None,
    denied_tools: Optional[Set[str]] = None,
    allowed_categories: Optional[List[ToolCategory]] = None,
) -> ToolFacade:
    """Create a new facade for standalone (session-free) usage with optional tool filtering.
    
    Args:
        workspace_root: Workspace root path.
        session_id: Optional session ID. If not provided, resolves from
                   NAGATO_SESSION_ID env var or defaults to "standalone".
                   Formatted IDs like "proj:my-app" are sanitized for filesystem use.
        allowed_tools: Allowlist of tool names (e.g., {"nagato_read_file"}).
        denied_tools: Denylist of tool names (e.g., {"nagato_shell"}). Takes precedence.
        allowed_categories: Allowlist of ToolCategory enums (e.g., [ToolCategory.READ]).
        
    Note: If no filter params provided, loads from .nagato/standalone.yaml if it exists.
    Explicit params override config file.
    """
    return ToolFacade(
        fsm_instance=None, 
        workspace_root=workspace_root,
        session_id=session_id,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
        allowed_categories=allowed_categories,
    )


def create_fsm_facade(fsm_instance: Any) -> ToolFacade:
    """Create a new facade bound to a session instance."""
    return ToolFacade(fsm_instance=fsm_instance)