/** Demo module documentation. */
import primary, {readFile as read, writeFile} from "fs";
import * as helpers from "helpers";
import "side-effect";
export {read as load};
export {remote as renamed} from "remote";
export * from "other";
export const [first, ...others] = values;
const {key: alias, shorthand, nested: {deep}, fallback = 1} = input;
export class Example extends Base {
  #secret = 1;
  static count = 0;
  [fieldKey] = 2;
  constructor(value = 1, ...rest) { this.value = value; }
  async run({name}, [item], flag = true, ...args) {
    function nestedFunction() { return name; }
    return nestedFunction();
  }
  static *items() { yield 1; }
  get value() { return this.#secret; }
  set value(v) { this.#secret = v; }
  [methodKey](x) { return x; }
  #privateMethod() {}
  callback = (x) => x;
}
const Expression = class Inner { constructor() {} method() {} };
const UnnamedClass = class { method() {} };
export async function declared(a, b = 1, ...rest) { return a; }
const namedExpression = function inner(x) { return x; };
const anonymous = function(x) { return x; };
const generator = function* () { yield 1; };
const arrow = async x => x;
const view = (props) => <h1 title="😀">{props.title}</h1>;
const object = { method(p) { return p; } };
// Ordinary comment
export default function () { return "é😀"; }
var oldStyle = 1;
let mutable = 2;
export * as namespace from "namespaced";
function* generated() { yield 1; }
function patterns({a: local = 2} = {}, ...rest) { return local; }
function scopeOne() { class Same { method() {} } }
function scopeTwo() { class Same { method() {} } }
