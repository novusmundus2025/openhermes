import {EditorView, basicSetup} from 'codemirror';
import {EditorState, Compartment} from '@codemirror/state';
import {keymap} from '@codemirror/view';
import {indentWithTab} from '@codemirror/commands';
import {syntaxTree} from '@codemirror/language';
import {javascript} from '@codemirror/lang-javascript';
import {json, jsonParseLinter} from '@codemirror/lang-json';
import {html} from '@codemirror/lang-html';
import {css} from '@codemirror/lang-css';
import {python} from '@codemirror/lang-python';
import {markdown} from '@codemirror/lang-markdown';
import {linter, lintGutter, openLintPanel, diagnosticCount} from '@codemirror/lint';
import {oneDark} from '@codemirror/theme-one-dark';
import {Linter} from 'eslint-linter-browserify';
import * as prettier from 'prettier/standalone';
import * as babel from 'prettier/plugins/babel';
import * as estree from 'prettier/plugins/estree';
import * as htmlPlugin from 'prettier/plugins/html';
import * as postcss from 'prettier/plugins/postcss';
import * as markdownPlugin from 'prettier/plugins/markdown';
import * as typescript from 'prettier/plugins/typescript';
import * as yaml from 'prettier/plugins/yaml';
const language = new Compartment(), checks = new Compartment();
const eslint = new Linter();
let view, path = '', parser = null, formatVersion = 0;
const plugins = [babel, estree, htmlPlugin, postcss, markdownPlugin, typescript, yaml];
function languageFor(file) {
 const ext=file.split('.').pop().toLowerCase();
 if(['js','jsx','mjs','cjs','ts','tsx'].includes(ext)) return {extension:javascript({jsx:ext.endsWith('x'),typescript:ext.startsWith('t')}),parser:ext.startsWith('t')?'typescript':'babel',js:!ext.startsWith('t'),ext};
 if(ext==='json') return {extension:json(),parser:'json',json:true};
 if(['html','htm','vue'].includes(ext))return {extension:html(),parser:ext==='vue'?'vue':'html'};
 if(['css','scss','less'].includes(ext))return {extension:css(),parser:ext};
 if(ext==='py')return {extension:python(),parser:null};
 if(['md','mdx'].includes(ext))return {extension:markdown(),parser:ext==='mdx'?'mdx':'markdown'};
 if(['yml','yaml'].includes(ext))return {extension:[],parser:'yaml'};
 return {extension:[],parser:null,plain:true};
}
function diagnostics(editor, spec) {
 if(spec.json)return jsonParseLinter()(editor);
 if(spec.js){
  const code=editor.state.doc.toString();
  const messages=eslint.verify(code,[{languageOptions:{ecmaVersion:'latest',sourceType:spec.ext==='cjs'?'commonjs':'module',parserOptions:{ecmaFeatures:{jsx:true}}},rules:{'no-unused-vars':'warn','no-unreachable':'error','no-dupe-args':'error','no-dupe-keys':'error','valid-typeof':'error','constructor-super':'error','no-constant-condition':'warn'}}]);
  return messages.map(m=>{const line=editor.state.doc.line(Math.min(m.line||1,editor.state.doc.lines));const from=Math.min(line.to,line.from+(m.column||1)-1);return {from,to:Math.min(editor.state.doc.length,from+1),severity:m.severity===2?'error':'warning',message:m.message,source:m.ruleId||'JavaScript'};});
 }
 const result=[];
 if(!spec.plain)syntaxTree(editor.state).iterate({enter(node){if(node.type.isError&&result.length<50)result.push({from:node.from,to:Math.min(editor.state.doc.length,Math.max(node.to,node.from+1)),severity:'error',message:'Syntax error near this location',source:'Parser'});}});
 return result;
}
function status(){
 if(!view)return;
 const count=diagnosticCount(view.state);
 document.getElementById('editor-problems').textContent=count?`${count} problem${count===1?'':'s'}`:'Problems';
 document.getElementById('editor-problems').classList.toggle('has-problems',count>0);
}
window.ProjectEditor={
 mount(){
  const textarea=document.getElementById('editor');
  const host=document.createElement('div');host.id='code-editor';textarea.parentElement.append(host);
  textarea.hidden=true;document.getElementById('line-numbers').hidden=true;
  view=new EditorView({parent:host,state:EditorState.create({extensions:[basicSetup,oneDark,keymap.of([indentWithTab,{key:'Mod-Shift-f',run:()=>{this.format();return true;}}]),language.of([]),checks.of([]),lintGutter(),EditorView.contentAttributes.of({'aria-label':'Code editor','spellcheck':'false'}),EditorView.updateListener.of(update=>{if(update.docChanged){textarea.value=update.state.doc.toString();textarea.dispatchEvent(new Event('input'));}status();}),EditorView.theme({'&':{height:'100%',fontSize:'12px',backgroundColor:'#11151c'},'.cm-scroller':{overflow:'auto',fontFamily:'Consolas, ui-monospace, monospace'},'.cm-gutters':{backgroundColor:'#141922',border:'none'},'.cm-content':{padding:'16px 0'},'.cm-focused':{outline:'none'}})]})});
  document.getElementById('format-code').onclick=()=>this.format();
  document.getElementById('editor-problems').onclick=()=>openLintPanel(view);
 },
 sync(file,content){
  if(!view)return;
  if(path!==file){path=file;formatVersion++;const spec=languageFor(file||'');parser=spec.parser;document.getElementById('format-code').disabled=!parser;document.getElementById('format-code').title=parser?'Format with Prettier (Ctrl+Shift+F)':'Formatting is unavailable for this file type';view.dispatch({effects:[language.reconfigure(spec.extension),checks.reconfigure(linter(v=>diagnostics(v,spec),{delay:400}))]});}
  if(view.state.doc.toString()!==content)view.dispatch({changes:{from:0,to:view.state.doc.length,insert:content}});
 },
 async format(){
  if(!parser)return;
  const original=view.state.doc.toString(),version=formatVersion;
  try{const formatted=await prettier.format(original,{parser,plugins,tabWidth:2});if(version!==formatVersion||view.state.doc.toString()!==original)return;view.dispatch({changes:{from:0,to:original.length,insert:formatted}});view.focus();document.getElementById('footer-detail').textContent='Formatted with Prettier · Save to keep changes';}
  catch(error){document.getElementById('notice').textContent='Cannot format: '+error.message;document.getElementById('notice').hidden=false;}
 }
};
