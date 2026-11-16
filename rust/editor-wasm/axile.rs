//! Axile's small LSP bridge; the upstream workspace owns parsing and inference.
use super::*;
use ruff_python_ast::{
    self as ast,
    visitor::{Visitor, walk_expr, walk_stmt},
};
use ruff_text_size::TextRange;
use serde_json::{Value, json};

fn position(value: Position) -> Value {
    json!({"line": value.line - 1, "character": value.column - 1})
}

fn range(value: Range) -> Value {
    json!({"start": position(value.start), "end": position(value.end)})
}

fn uri(path: &str) -> String {
    if path.starts_with("vendored:") {
        path.to_owned()
    } else {
        format!("file://{path}")
    }
}

fn text_edit(edit: TextEdit) -> Value {
    json!({"range": range(edit.range), "newText": edit.new_text})
}

fn point(value: &Value) -> Result<Position, Error> {
    let line = value["line"]
        .as_u64()
        .ok_or_else(|| Error::new("Invalid line"))?;
    let column = value["character"]
        .as_u64()
        .ok_or_else(|| Error::new("Invalid character"))?;
    Ok(Position::new(line as usize + 1, column as usize + 1))
}

impl Workspace {
    fn lsp_diagnostic(&self, diagnostic: &Diagnostic) -> Value {
        let severity = match diagnostic.severity() {
            Severity::Error | Severity::Fatal => 1,
            Severity::Warning => 2,
            _ => 3,
        };
        json!({"range": diagnostic.to_range(self).map(range), "severity": severity,
            "message": diagnostic.display(self).as_string().unwrap_or_default(),
            "code": diagnostic.id().as_string(), "source": "ty"})
    }

    fn symbols(&self, file: &FileHandle) -> Value {
        let symbols =
            ty_ide::document_symbols(&self.db, self.db.program_file(file.file)).to_hierarchical();
        let source = source_text(&self.db, file.file);
        let index = line_index(&self.db, file.file);
        fn children<'a>(
            workspace: &Workspace,
            symbols: &'a ty_ide::HierarchicalSymbols,
            items: impl Iterator<Item = (ty_ide::SymbolId, ty_ide::SymbolInfo<'a>)>,
            source: &str,
            index: &LineIndex,
        ) -> Vec<Value> {
            items.map(|(id, symbol)| {
                let kind = match symbol.kind {
                    ty_ide::SymbolKind::Module | ty_ide::SymbolKind::Import => 2,
                    ty_ide::SymbolKind::Class => 5, ty_ide::SymbolKind::Method => 6,
                    ty_ide::SymbolKind::Property => 7, ty_ide::SymbolKind::Field => 8,
                    ty_ide::SymbolKind::Constructor => 9, ty_ide::SymbolKind::Function => 12,
                    ty_ide::SymbolKind::Constant => 14, ty_ide::SymbolKind::TypeParameter => 26, _ => 13,
                };
                json!({"name": symbol.name, "kind": kind,
                    "range": range(Range::from_text_range(symbol.full_range, index, source, workspace.position_encoding)),
                    "selectionRange": range(Range::from_text_range(symbol.name_range, index, source, workspace.position_encoding)),
                    "children": children(workspace, symbols, symbols.children(id), source, index)})
            }).collect()
        }
        json!(children(self, &symbols, symbols.iter(), &source, &index))
    }
}

#[wasm_bindgen]
impl Workspace {
    /// JSON keeps protocol values independent of wasm-bindgen object lifetimes.
    #[wasm_bindgen(js_name = "axileRequest")]
    pub fn axile_request(
        &self,
        method: &str,
        file: &FileHandle,
        raw: &str,
    ) -> Result<String, Error> {
        let params: Value = serde_json::from_str(raw).map_err(into_error)?;
        let result = match method {
            "textDocument/diagnostic" => json!({"kind": "full", "items": self.check_file(file)?.iter()
                .map(|item| self.lsp_diagnostic(item)).filter(|item| !item["range"].is_null()).collect::<Vec<_>>() }),
            "textDocument/hover" => self.hover(file, point(&params["position"])?)?
                .map(|item| json!({"contents": {"kind": "markdown", "value": item.markdown}, "range": range(item.range)})).unwrap_or(Value::Null),
            "textDocument/completion" => json!(self.completions(file, point(&params["position"])?)?.into_iter().map(|item| {
                let label = item.module_name.as_ref().map_or_else(|| item.name.clone(), |module| format!("{} ({module})", item.name));
                json!({"label": label, "kind": item.kind.map(|kind| kind as u32 + 1), "detail": item.detail,
                    "insertText": item.insert_text.unwrap_or(item.name), "documentation": item.documentation,
                    "additionalTextEdits": item.additional_text_edits.unwrap_or_default().into_iter().map(text_edit).collect::<Vec<_>>()})
            }).collect::<Vec<_>>()),
            "textDocument/definition" | "textDocument/references" => {
                let point = point(&params["position"])?;
                let locations = if method.ends_with("definition") { self.goto_definition(file, point)? }
                    else { self.goto_references(file, point)? };
                json!(locations.into_iter().map(|item| json!({"uri": uri(&item.path),
                    "range": range(item.selection_range.unwrap_or(item.full_range))})).collect::<Vec<_>>())
            }
            "textDocument/prepareRename" => self.prepare_rename(file, point(&params["position"])?)?.map(range).unwrap_or(Value::Null),
            "textDocument/rename" => {
                let mut changes = serde_json::Map::<String, Value>::new();
                let name = params["newName"].as_str().ok_or_else(|| Error::new("Invalid name"))?;
                for item in self.rename(file, point(&params["position"])?, name)? {
                    changes.entry(uri(&item.path)).or_insert_with(|| json!([])).as_array_mut().unwrap()
                        .push(json!({"range": range(item.range), "newText": item.new_text}));
                }
                json!({"changes": changes})
            }
            "textDocument/codeAction" => {
                let mut actions = Vec::new();
                for diagnostic in self.check_file(file)? {
                    for action in self.code_actions(file, &diagnostic).unwrap_or_default() {
                        actions.push(json!({"title": action.title, "kind": "quickfix", "isPreferred": action.preferred,
                            "diagnostics": [self.lsp_diagnostic(&diagnostic)],
                            "edit": {"changes": {uri(&file.path.to_string()): action.edits.into_iter().map(text_edit).collect::<Vec<_>>()}}}));
                    }
                }
                json!(actions)
            }
            "textDocument/signatureHelp" => self.signature_help(file, point(&params["position"])?)?
                .map(|help| json!({"activeSignature": help.active_signature, "signatures": help.signatures.into_iter().map(|item| json!({
                    "label": item.label, "documentation": item.documentation, "activeParameter": item.active_parameter,
                    "parameters": item.parameters.into_iter().map(|param| json!({"label": param.label, "documentation": param.documentation})).collect::<Vec<_>>()
                })).collect::<Vec<_>>()})).unwrap_or(Value::Null),
            "textDocument/semanticTokens/full" => {
                let mut data = Vec::new();
                let (mut line, mut column) = (0, 0);
                for token in self.semantic_tokens(file)? {
                    let start = token.range.start;
                    if start.line != token.range.end.line { continue; }
                    let next_line = start.line - 1;
                    let next_column = start.column - 1;
                    data.extend([next_line - line, if next_line == line { next_column.saturating_sub(column) } else { next_column },
                        token.range.end.column - start.column, token.kind as usize, token.modifiers as usize]);
                    (line, column) = (next_line, next_column);
                }
                json!({"data": data})
            }
            "textDocument/inlayHint" => json!(self.inlay_hints(file, Range::new(point(&params["range"]["start"])?, point(&params["range"]["end"]) ?))?
                .into_iter().map(|item| json!({"position": position(item.position), "kind": item.kind as u32 + 1,
                    "label": item.label.into_iter().map(|part| part.label).collect::<String>(),
                    "textEdits": item.text_edits.into_iter().map(text_edit).collect::<Vec<_>>() })).collect::<Vec<_>>()),
            "textDocument/documentSymbol" => self.symbols(file),
            "textDocument/foldingRange" => {
                let source = source_text(&self.db, file.file);
                let index = line_index(&self.db, file.file);
                json!(ty_ide::folding_ranges(&self.db, self.db.program_file(file.file).python_file(&self.db), None).into_iter().map(|item| {
                    let value = Range::from_text_range(item.range, &index, &source, self.position_encoding);
                    json!({"startLine": value.start.line - 1, "startCharacter": value.start.column - 1,
                        "endLine": value.end.line - 1, "endCharacter": value.end.column - 1})
                }).collect::<Vec<_>>())
            }
            "axile/format" => json!({"code": self.format(file)?.ok_or_else(|| Error::new("无法格式化存在语法错误的代码"))?}),
            "axile/source" => json!({"code": self.source_text(file)?}),
            "axile/contract" => contract(&self.source_text(file)?, params["kind"].as_str().unwrap_or(""))?,
            _ => return Err(Error::new(&format!("Unsupported method: {method}"))),
        };
        serde_json::to_string(&result).map_err(into_error)
    }
}

struct YieldVisitor(bool);
impl<'a> Visitor<'a> for YieldVisitor {
    fn visit_stmt(&mut self, statement: &'a ast::Stmt) {
        if !matches!(
            statement,
            ast::Stmt::FunctionDef(_) | ast::Stmt::ClassDef(_)
        ) {
            walk_stmt(self, statement);
        }
    }
    fn visit_expr(&mut self, expression: &'a ast::Expr) {
        match expression {
            ast::Expr::Yield(_) | ast::Expr::YieldFrom(_) => self.0 = true,
            ast::Expr::Lambda(_) => {}
            _ => walk_expr(self, expression),
        }
    }
}

fn contract(code: &str, kind: &str) -> Result<Value, Error> {
    let (name, module, type_name) = match kind {
        "portfolio" => ("calculate_portfolio", "axile.server.context", "Context"),
        "account_notification" => (
            "notify",
            "axile.common.notification_context",
            "AccountNotificationContext",
        ),
        "system_notification" => (
            "notify",
            "axile.common.notification_context",
            "SystemNotificationContext",
        ),
        _ => return Err(Error::new("无效编辑器类型")),
    };
    let mut diagnostics = Vec::new();
    let mut fixes = Vec::new();
    let report = |diagnostics: Vec<Value>, fixes: Vec<Value>| json!({"diagnostics": diagnostics, "fixes": fixes});
    let Ok(parsed) = ruff_python_parser::parse_module(code) else {
        return Ok(report(diagnostics, fixes));
    };
    let index = LineIndex::from_source_text(code);
    let to_range = |value| {
        range(Range::from_text_range(
            value,
            &index,
            code,
            PositionEncoding::Utf16,
        ))
    };
    let diagnostic = |message: String, value: TextRange, severity| json!({"range": to_range(value), "message": message, "severity": severity, "source": "入口契约"});
    let Some(function) = parsed
        .syntax()
        .body
        .iter()
        .rev()
        .find_map(|statement| match statement {
            ast::Stmt::FunctionDef(function) if function.name.as_str() == name => Some(function),
            _ => None,
        })
    else {
        diagnostics.push(diagnostic(
            format!("必须定义 {name}(context) 函数"),
            TextRange::empty(TextSize::new(0)),
            1,
        ));
        return Ok(report(diagnostics, fixes));
    };
    let start = function.start();
    if kind == "portfolio" && function.is_async {
        diagnostics.push(diagnostic(
            format!("{name} 必须是同步函数"),
            TextRange::at(start, TextSize::new(9)),
            1,
        ));
    }
    let mut visitor = YieldVisitor(false);
    for statement in &function.body {
        visitor.visit_stmt(statement);
    }
    if kind != "portfolio" && visitor.0 {
        diagnostics.push(diagnostic(
            "notify 不支持生成器或异步生成器函数".to_owned(),
            TextRange::at(start, TextSize::new(name.len() as u32)),
            1,
        ));
    }
    let parameters = &function.parameters;
    if parameters.posonlyargs.len() + parameters.args.len() != 1
        || parameters.vararg.is_some()
        || parameters.kwarg.is_some()
        || !parameters.kwonlyargs.is_empty()
    {
        diagnostics.push(diagnostic(
            format!("{name} 必须且只能接收一个位置参数 context"),
            TextRange::at(start, TextSize::new(name.len() as u32)),
            1,
        ));
        return Ok(report(diagnostics, fixes));
    }
    let argument = &parameters
        .posonlyargs
        .iter()
        .chain(parameters.args.iter())
        .next()
        .unwrap()
        .parameter;
    if argument.annotation.is_none() {
        diagnostics.push(diagnostic(
            format!(
                "可为 {} 标注 {type_name}，以获得类型检查和补全",
                argument.name
            ),
            argument.name.range(),
            3,
        ));
        let mut edits = vec![
            json!({"range": to_range(TextRange::empty(argument.name.end())), "newText": format!(": {type_name}")}),
        ];
        let imported = parsed.syntax().body.iter().any(|statement| matches!(statement,
            ast::Stmt::ImportFrom(import) if import.module.as_ref().is_some_and(|value| value.as_str() == module) && import.level == 0 &&
                import.names.iter().any(|alias| alias.name.as_str() == type_name && alias.asname.is_none())));
        if !imported {
            let mut insert_line = 0;
            for (i, statement) in parsed.syntax().body.iter().enumerate() {
                let docstring = i == 0
                    && matches!(statement, ast::Stmt::Expr(expr) if matches!(expr.value.as_ref(), ast::Expr::StringLiteral(_)));
                let future = matches!(statement, ast::Stmt::ImportFrom(import) if import.module.as_ref().is_some_and(|value| value.as_str() == "__future__"));
                if docstring || future {
                    insert_line = Range::from_text_range(
                        statement.range(),
                        &index,
                        code,
                        PositionEncoding::Utf16,
                    )
                    .end
                    .line;
                }
            }
            let at = json!({"line": insert_line, "character": 0});
            edits.push(json!({"range": {"start": at, "end": at}, "newText": format!("from {module} import {type_name}\n")}));
        }
        fixes.push(json!({"title": format!("为 {} 添加 {type_name} 类型标注", argument.name), "edits": edits}));
    }
    Ok(report(diagnostics, fixes))
}
